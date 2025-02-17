import torch
import torch.nn as nn

from equivariant_attention.modules import get_basis_and_r, GSE3Res, GNormBias
from equivariant_attention.modules import GConvSE3, GNormSE3
from equivariant_attention.fibers import Fiber

class TFN(nn.Module):
    """SE(3) equivariant GCN"""
    def __init__(self, num_layers=2, num_channels=32, num_nonlin_layers=1, num_degrees=3, 
                 l0_in_features=32, l0_out_features=32,
                 l1_in_features=3, l1_out_features=3,
                 num_edge_features=32, use_self=True):
        super().__init__()
        # Build the network
        self.num_layers = num_layers
        self.num_nlayers = num_nonlin_layers
        self.num_channels = num_channels
        self.num_degrees = num_degrees
        self.edge_dim = num_edge_features
        self.use_self = use_self

        if l1_out_features > 0:
            fibers = {'in': Fiber(dictionary={0: l0_in_features, 1: l1_in_features}),
                           'mid': Fiber(self.num_degrees, self.num_channels),
                           'out': Fiber(dictionary={0: l0_out_features, 1: l1_out_features})}
        else:
            fibers = {'in': Fiber(dictionary={0: l0_in_features, 1: l1_in_features}),
                           'mid': Fiber(self.num_degrees, self.num_channels),
                           'out': Fiber(dictionary={0: l0_out_features})}
        blocks = self._build_gcn(fibers)
        self.block0 = blocks

    def _build_gcn(self, fibers):

        block0 = []
        fin = fibers['in']
        for i in range(self.num_layers-1):
            block0.append(GConvSE3(fin, fibers['mid'], self_interaction=self.use_self, edge_dim=self.edge_dim))
            block0.append(GNormSE3(fibers['mid'], num_layers=self.num_nlayers))
            fin = fibers['mid']
        block0.append(GConvSE3(fibers['mid'], fibers['out'], self_interaction=self.use_self, edge_dim=self.edge_dim))
        return nn.ModuleList(block0)

    @torch.cuda.amp.autocast(enabled=True)
    def forward(self, G, type_0_features, type_1_features):
        # Compute equivariant weight basis from relative positions
        basis, r = get_basis_and_r(G, self.num_degrees-1)
        h = {'0': type_0_features, '1': type_1_features}
        for layer in self.block0:
            h = layer(h, G=G, r=r, basis=basis)
        return h  

class SE3Transformer(nn.Module):
    """SE(3) equivariant GCN with attention"""
    def __init__(self, num_layers=2, num_channels=32, num_degrees=3, n_heads=4, div=4,
                 si_m='1x1', si_e='att',
                 l0_in_features=32, l0_out_features=32,
                 l1_in_features=3, l1_out_features=3,
                 num_edge_features=32, x_ij=None):
        super().__init__()
        # Build the network
        self.num_layers = num_layers
        self.num_channels = num_channels
        self.num_degrees = num_degrees
        self.edge_dim = num_edge_features
        self.div = div
        self.n_heads = n_heads
        self.si_m, self.si_e = si_m, si_e
        self.x_ij = x_ij

        # fiber 不懂是用来做什么的

        if l1_out_features > 0:
            fibers = {'in': Fiber(dictionary={0: l0_in_features, 1: l1_in_features}),
                           'mid': Fiber(self.num_degrees, self.num_channels),
                           'out': Fiber(dictionary={0: l0_out_features, 1: l1_out_features})}
        else:
            fibers = {'in': Fiber(dictionary={0: l0_in_features, 1: l1_in_features}),
                           'mid': Fiber(self.num_degrees, self.num_channels),
                           'out': Fiber(dictionary={0: l0_out_features})}

        blocks = self._build_gcn(fibers)
        self.Gblock = blocks

    def _build_gcn(self, fibers):
        # Equivariant layers
        Gblock = []
        # 在这里应该是串起来了 in mid mid mid mid mid out
        fin = fibers['in']
        for i in range(self.num_layers):
            Gblock.append(GSE3Res(fin, fibers['mid'], edge_dim=self.edge_dim,
                                  div=self.div, n_heads=self.n_heads,
                                  learnable_skip=True, skip='cat',
                                  selfint=self.si_m, x_ij=self.x_ij))
            Gblock.append(GNormBias(fibers['mid']))
            fin = fibers['mid']
        Gblock.append(
            GSE3Res(fibers['mid'], fibers['out'], edge_dim=self.edge_dim,
                    div=1, n_heads=min(1, 2), learnable_skip=True,
                    skip='cat', selfint=self.si_e, x_ij=self.x_ij))
        return nn.ModuleList(Gblock)

    @torch.cuda.amp.autocast(enabled=False)
    def forward(self, G, type_0_features, type_1_features):
        # Compute equivariant weight basis from relative positions
        # type_0_features type_1_features 都是点特征
        # G里边是点和边的特征
        # type_0_features msa特征 需要算梯度
        # type_1_features 向量信息 需要算梯度
        # degree 的作用一直没想明白
        # basis 是根据度数存的信息 dict_keys(['0,0', '0,1', '1,0', '1,1'])
        basis, r = get_basis_and_r(G, self.num_degrees-1)
        # print("SE3Transformer", basis.keys(), r.requires_grad)
        # r 只是单纯的用边的xyz距离信息算了个综合距离 r = sqrt(x * x + y * y + z * z)
        # basis 比较复杂， 计算的是球面谐波的一些信息，看注释说的是旋转不变的信息， 不知道怎么做的
        # basie 使用了一些默认的参数，这些参数被放在了文件里
        # basis最终是在PairwiseConv 中使用， 看名字是卷积
        # 理解没错的话， basis和r都是参考信息， 不需要做梯度的。
        # print("debuggggg", G.ndata.keys(), G.edata.keys())
        h = {'0': type_0_features, '1': type_1_features}

        for layer in self.Gblock:
            h = layer(h, G=G, r=r, basis=basis)
        # print(f"forward {h['0'].shape} {h['1'].shape}")
        return h
