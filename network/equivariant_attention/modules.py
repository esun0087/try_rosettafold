import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from contextlib import nullcontext

from typing import Dict

from equivariant_attention.from_se3cnn import utils_steerable
from equivariant_attention.fibers import Fiber, fiber2head
from utils.utils_logging import log_gradient_norm

import dgl
import dgl.function as fn
from dgl.nn.pytorch.softmax import edge_softmax
from dgl.nn.pytorch.glob import AvgPooling, MaxPooling

from packaging import version


def get_basis(G, max_degree, compute_gradients):
    """Precompute the SE(3)-equivariant weight basis, W_J^lk(x)

    This is called by get_basis_and_r().

    Args:
        G: DGL graph instance of type dgl.DGLGraph
        max_degree: non-negative int for degree of highest feature type
        compute_gradients: boolean, whether to compute gradients during basis construction
    Returns:
        dict of equivariant bases. Keys are in the form 'd_in,d_out'. Values are
        tensors of shape (batch_size, 1, 2*d_out+1, 1, 2*d_in+1, number_of_bases)
        where the 1's will later be broadcast to the number of output and input
        channels
    """
    if compute_gradients:
        context = nullcontext()
    else:
        context = torch.no_grad()

    with context:
        cloned_d = torch.clone(G.edata['d'])

        if G.edata['d'].requires_grad:
            cloned_d.requires_grad_()
            log_gradient_norm(cloned_d, 'Basis computation flow')

        # Relative positional encodings (vector)
        r_ij = utils_steerable.get_spherical_from_cartesian_torch(cloned_d)
        # Spherical harmonic basis
        Y = utils_steerable.precompute_sh(r_ij, 2*max_degree)
        # print(cloned_d.shape, r_ij.shape, Y.keys()  )
        device = Y[0].device

        basis = {}
        for d_in in range(max_degree+1):
            for d_out in range(max_degree+1):
                K_Js = []
                for J in range(abs(d_in-d_out), d_in+d_out+1):
                    # Get spherical harmonic projection matrices
                    Q_J = utils_steerable._basis_transformation_Q_J(J, d_in, d_out)
                    Q_J = Q_J.float().T.to(device)

                    # Create kernel from spherical harmonics
                    K_J = torch.matmul(Y[J], Q_J)
                    K_Js.append(K_J)

                # Reshape so can take linear combinations with a dot product
                size = (-1, 1, 2*d_out+1, 1, 2*d_in+1, 2*min(d_in, d_out)+1)
                basis[f'{d_in},{d_out}'] = torch.stack(K_Js, -1).view(*size)
        return basis


def get_r(G):
    """Compute internodal distances"""
    cloned_d = torch.clone(G.edata['d'])

    if G.edata['d'].requires_grad:
        cloned_d.requires_grad_()
        log_gradient_norm(cloned_d, 'Neural networks flow')

    return torch.sqrt(torch.sum(cloned_d**2, -1, keepdim=True))


def get_basis_and_r(G, max_degree, compute_gradients=False):
    """Return equivariant weight basis (basis) and internodal distances (r).

    Call this function *once* at the start of each forward pass of the model.
    It computes the equivariant weight basis, W_J^lk(x), and internodal
    distances, needed to compute varphi_J^lk(x), of eqn 8 of
    https://arxiv.org/pdf/2006.10503.pdf. The return values of this function
    can be shared as input across all SE(3)-Transformer layers in a model.

    Args:
        G: DGL graph instance of type dgl.DGLGraph()
        max_degree: non-negative int for degree of highest feature-type
        compute_gradients: controls whether to compute gradients during basis construction
    Returns:
        dict of equivariant bases, keys are in form '<d_in><d_out>'
        vector of relative distances, ordered according to edge ordering of G
    """
    #  暂时不懂这个返回的是什么
    # 不过这个用的也是edata['d'], 感觉还是根据距离计算了一些信息,或者说两个数据之间的坐标向量差值
    basis = get_basis(G, max_degree, compute_gradients)
    # 只是单纯的一个计算距离的操作， 因此返回的是L * L, 
    # edata['d']存储的是x y z之间的差距，这里只是直接计算了一个和
    r = get_r(G) 
    return basis, r


### SE(3) equivariant operations on graphs in DGL

class GConvSE3(nn.Module):
    """A tensor field network layer as a DGL module.

    GConvSE3 stands for a Graph Convolution SE(3)-equivariant layer. It is the
    equivalent of a linear layer in an MLP, a conv layer in a CNN, or a graph
    conv layer in a GCN.

    At each node, the activations are split into different "feature types",
    indexed by the SE(3) representation type: non-negative integers 0, 1, 2, ..
    """
    def __init__(self, f_in, f_out, self_interaction: bool=False, edge_dim: int=0, flavor='skip'):
        """SE(3)-equivariant Graph Conv Layer

        Args:
            f_in: list of tuples [(multiplicities, type),...]
            f_out: list of tuples [(multiplicities, type),...]
            self_interaction: include self-interaction in convolution
            edge_dim: number of dimensions for edge embedding
            flavor: allows ['TFN', 'skip'], where 'skip' adds a skip connection
        """
        super().__init__()
        self.f_in = f_in
        self.f_out = f_out
        self.edge_dim = edge_dim
        self.self_interaction = self_interaction
        self.flavor = flavor

        # Neighbor -> center weights
        self.kernel_unary = nn.ModuleDict()
        for (mi, di) in self.f_in.structure:
            for (mo, do) in self.f_out.structure:
                self.kernel_unary[f'({di},{do})'] = PairwiseConv(di, mi, do, mo, edge_dim=edge_dim)

        # Center -> center weights
        self.kernel_self = nn.ParameterDict()
        if self_interaction:
            assert self.flavor in ['TFN', 'skip']
            if self.flavor == 'TFN':
                for m_out, d_out in self.f_out.structure:
                    W = nn.Parameter(torch.randn(1, m_out, m_out) / np.sqrt(m_out))
                    self.kernel_self[f'{d_out}'] = W
            elif self.flavor == 'skip':
                for m_in, d_in in self.f_in.structure:
                    if d_in in self.f_out.degrees:
                        m_out = self.f_out.structure_dict[d_in]
                        W = nn.Parameter(torch.randn(1, m_out, m_in) / np.sqrt(m_in))
                        self.kernel_self[f'{d_in}'] = W



    def __repr__(self):
        return f'GConvSE3(structure={self.f_out}, self_interaction={self.self_interaction})'


    def udf_u_mul_e(self, d_out):
        """Compute the convolution for a single output feature type.

        This function is set up as a User Defined Function in DGL.

        Args:
            d_out: output feature type
        Returns:
            edge -> node function handle
        """
        def fnc(edges):
            # Neighbor -> center messages
            msg = 0
            for m_in, d_in in self.f_in.structure:
                src = edges.src[f'{d_in}'].view(-1, m_in*(2*d_in+1), 1)
                edge = edges.data[f'({d_in},{d_out})']
                msg = msg + torch.matmul(edge, src)
            msg = msg.view(msg.shape[0], -1, 2*d_out+1)

            # Center -> center messages
            if self.self_interaction:
                if f'{d_out}' in self.kernel_self.keys():
                    if self.flavor == 'TFN':
                        W = self.kernel_self[f'{d_out}']
                        msg = torch.matmul(W, msg)
                    if self.flavor == 'skip':
                        dst = edges.dst[f'{d_out}']
                        W = self.kernel_self[f'{d_out}']
                        msg = msg + torch.matmul(W, dst)

            return {'msg': msg.view(msg.shape[0], -1, 2*d_out+1)}
        return fnc

    def forward(self, h, G=None, r=None, basis=None, **kwargs):
        """Forward pass of the linear layer

        Args:
            G: minibatch of (homo)graphs
            h: dict of features
            r: inter-atomic distances
            basis: pre-computed Q * Y
        Returns:
            tensor with new features [B, n_points, n_features_out]
        """
        with G.local_scope():
            # Add node features to local graph scope
            for k, v in h.items():
                G.ndata[k] = v

            # Add edge features
            if 'w' in G.edata.keys():
                w = G.edata['w']
                feat = torch.cat([w, r], -1)
            else:
                feat = torch.cat([r, ], -1)

            for (mi, di) in self.f_in.structure:
                for (mo, do) in self.f_out.structure:
                    etype = f'({di},{do})'
                    G.edata[etype] = self.kernel_unary[etype](feat, basis)

            # Perform message-passing for each output feature type
            for d in self.f_out.degrees:
                G.update_all(self.udf_u_mul_e(d), fn.mean('msg', f'out{d}'))

            return {f'{d}': G.ndata[f'out{d}'] for d in self.f_out.degrees}


class RadialFunc(nn.Module):
    """NN parameterized radial profile function."""
    def __init__(self, num_freq, in_dim, out_dim, edge_dim: int=0):
        """NN parameterized radial profile function.

        Args:
            num_freq: number of output frequencies
            in_dim: multiplicity of input (num input channels)
            out_dim: multiplicity of output (num output channels)
            edge_dim: number of dimensions for edge embedding
        """
        super().__init__()
        self.num_freq = num_freq
        self.in_dim = in_dim
        self.mid_dim = 32
        self.out_dim = out_dim
        self.edge_dim = edge_dim

        self.net = nn.Sequential(nn.Linear(self.edge_dim+1,self.mid_dim),
                                 BN(self.mid_dim),
                                 nn.ReLU(),
                                 nn.Linear(self.mid_dim,self.mid_dim),
                                 BN(self.mid_dim),
                                 nn.ReLU(),
                                 nn.Linear(self.mid_dim,self.num_freq*in_dim*out_dim))

        nn.init.kaiming_uniform_(self.net[0].weight)
        nn.init.kaiming_uniform_(self.net[3].weight)
        nn.init.kaiming_uniform_(self.net[6].weight)

    def __repr__(self):
        return f"RadialFunc(edge_dim={self.edge_dim}, in_dim={self.in_dim}, out_dim={self.out_dim})"

    def forward(self, x):
        y = self.net(x)
        return y.view(-1, self.out_dim, 1, self.in_dim, 1, self.num_freq)


class PairwiseConv(nn.Module):
    """SE(3)-equivariant convolution between two single-type features"""
    def __init__(self, degree_in: int, nc_in: int, degree_out: int,
                 nc_out: int, edge_dim: int=0):
        """SE(3)-equivariant convolution between a pair of feature types.

        This layer performs a convolution from nc_in features of type degree_in
        to nc_out features of type degree_out.

        Args:
            degree_in: degree of input fiber
            nc_in: number of channels on input
            degree_out: degree of out order
            nc_out: number of channels on output
            edge_dim: number of dimensions for edge embedding
        """
        super().__init__()
        # Log settings
        self.degree_in = degree_in
        self.degree_out = degree_out
        self.nc_in = nc_in
        self.nc_out = nc_out

        # Functions of the degree
        self.num_freq = 2*min(degree_in, degree_out) + 1
        self.d_out = 2*degree_out + 1
        self.edge_dim = edge_dim

        # Radial profile function
        self.rp = RadialFunc(self.num_freq, nc_in, nc_out, self.edge_dim)

    def forward(self, feat, basis):
        # Get radial weights
        # 感觉是用权重对feat进行重新处理了
        # rp不过也就是个全连接而已
        # feat是边特征，莫非是对边做权重
        R = self.rp(feat)
        kernel = torch.sum(R * basis[f'{self.degree_in},{self.degree_out}'], -1)
        # print("in PairwiseConv", feat.shape, self.rp, R.shape, basis[f'{self.degree_in},{self.degree_out}'].shape, kernel.shape)
        return kernel.view(kernel.shape[0], self.d_out*self.nc_out, -1)


class G1x1SE3(nn.Module):
    """Graph Linear SE(3)-equivariant layer, equivalent to a 1x1 convolution.

    This is equivalent to a self-interaction layer in TensorField Networks.
    """
    def __init__(self, f_in, f_out, learnable=True):
        """SE(3)-equivariant 1x1 convolution.

        Args:
            f_in: input Fiber() of feature multiplicities and types
            f_out: output Fiber() of feature multiplicities and types
        """
        super().__init__()
        self.f_in = f_in
        self.f_out = f_out

        # Linear mappings: 1 per output feature type
        self.transform = nn.ParameterDict()
        for m_out, d_out in self.f_out.structure:
            m_in = self.f_in.structure_dict[d_out]
            self.transform[str(d_out)] = nn.Parameter(torch.randn(m_out, m_in) / np.sqrt(m_in), requires_grad=learnable)

    def __repr__(self):
         return f"G1x1SE3(structure={self.f_out})"

    def forward(self, features, **kwargs):
        output = {}
        for k, v in features.items():
            if str(k) in self.transform.keys():
                output[k] = torch.matmul(self.transform[str(k)], v)
        return output


class GNormBias(nn.Module):
    """Norm-based SE(3)-equivariant nonlinearity with only learned biases."""

    def __init__(self, fiber, nonlin=nn.ReLU(),
                 num_layers: int = 0):
        """Initializer.

        Args:
            fiber: Fiber() of feature multiplicities and types
            nonlin: nonlinearity to use everywhere
            num_layers: non-negative number of linear layers in fnc
        """
        super().__init__()
        self.fiber = fiber
        self.nonlin = nonlin
        self.num_layers = num_layers

        # Regularization for computing phase: gradients explode otherwise
        self.eps = 1e-12

        # Norm mappings: 1 per feature type
        self.bias = nn.ParameterDict()
        for m, d in self.fiber.structure:
            self.bias[str(d)] = nn.Parameter(torch.randn(m).view(1, m))

    def __repr__(self):
        return f"GNormTFN()"


    def forward(self, features, **kwargs):
        output = {}
        for k, v in features.items():
            # Compute the norms and normalized features
            # v shape: [...,m , 2*k+1]
            norm = v.norm(2, -1, keepdim=True).clamp_min(self.eps).expand_as(v)
            phase = v / norm

            # Transform on norms
            # transformed = self.transform[str(k)](norm[..., 0]).unsqueeze(-1)
            transformed = self.nonlin(norm[..., 0] + self.bias[str(k)])

            # Nonlinearity on norm
            output[k] = (transformed.unsqueeze(-1) * phase).view(*v.shape)

        return output


class GAttentiveSelfInt(nn.Module):

    def __init__(self, f_in, f_out):
        """SE(3)-equivariant 1x1 convolution.

        Args:
            f_in: input Fiber() of feature multiplicities and types
            f_out: output Fiber() of feature multiplicities and types
        """
        super().__init__()
        self.f_in = f_in
        self.f_out = f_out
        self.nonlin = nn.LeakyReLU()
        self.num_layers = 2
        self.eps = 1e-12 # regularisation for phase: gradients explode otherwise

        # one network for attention weights per degree
        self.transform = nn.ModuleDict()
        for o, m_in in self.f_in.structure_dict.items():
            m_out = self.f_out.structure_dict[o]
            self.transform[str(o)] = self._build_net(m_in, m_out)

    def __repr__(self):
        return f"AttentiveSelfInteractionSE3(in={self.f_in}, out={self.f_out})"

    def _build_net(self, m_in: int, m_out):
        n_hidden = m_in * m_out
        cur_inpt = m_in * m_in
        net = []
        for i in range(1, self.num_layers):
            net.append(nn.LayerNorm(int(cur_inpt)))
            net.append(self.nonlin)
            # TODO: implement cleaner init
            net.append(
                nn.Linear(cur_inpt, n_hidden, bias=(i == self.num_layers - 1)))
            nn.init.kaiming_uniform_(net[-1].weight)
            cur_inpt = n_hidden
        return nn.Sequential(*net)

    def forward(self, features, **kwargs):
        output = {}
        for k, v in features.items():
            # v shape: [..., m, 2*k+1]
            first_dims = v.shape[:-2]
            m_in  = self.f_in.structure_dict[int(k)]
            m_out = self.f_out.structure_dict[int(k)]
            assert v.shape[-2] == m_in
            assert v.shape[-1] == 2 * int(k) + 1

            # Compute the norms and normalized features
            #norm = v.norm(p=2, dim=-1, keepdim=True).clamp_min(self.eps).expand_as(v)
            #phase = v / norm # [..., m, 2*k+1]
            scalars = torch.einsum('...ac,...bc->...ab', [v, v]) # [..., m_in, m_in]
            scalars = scalars.view(*first_dims, m_in*m_in) # [..., m_in*m_in]
            sign = scalars.sign()
            scalars = scalars.abs_().clamp_min(self.eps)
            scalars = scalars * sign

            # perform attention
            att_weights = self.transform[str(k)](scalars) # [..., m_out*m_in]
            att_weights = att_weights.view(*first_dims, m_out, m_in) # [..., m_out, m_in]
            att_weights = F.softmax(input=att_weights, dim=-1)
            # shape [..., m_out, 2*k+1]
            # output[k] = torch.einsum('...nm,...md->...nd', [att_weights, phase])
            output[k] = torch.einsum('...nm,...md->...nd', [att_weights, v])

        return output



class GNormSE3(nn.Module):
    """Graph Norm-based SE(3)-equivariant nonlinearity.

    Nonlinearities are important in SE(3) equivariant GCNs. They are also quite
    expensive to compute, so it is convenient for them to share resources with
    other layers, such as normalization. The general workflow is as follows:

    > for feature type in features:
    >    norm, phase <- feature
    >    output = fnc(norm) * phase

    where fnc: {R+}^m -> R^m is a learnable map from m norms to m scalars.
    """
    def __init__(self, fiber, nonlin=nn.ReLU(), num_layers: int=0):
        """Initializer.

        Args:
            fiber: Fiber() of feature multiplicities and types
            nonlin: nonlinearity to use everywhere
            num_layers: non-negative number of linear layers in fnc
        """
        super().__init__()
        self.fiber = fiber
        self.nonlin = nonlin
        self.num_layers = num_layers

        # Regularization for computing phase: gradients explode otherwise
        self.eps = 1e-12

        # Norm mappings: 1 per feature type
        self.transform = nn.ModuleDict()
        for m, d in self.fiber.structure:
            self.transform[str(d)] = self._build_net(int(m))

    def __repr__(self):
         return f"GNormSE3(num_layers={self.num_layers}, nonlin={self.nonlin})"

    def _build_net(self, m: int):
        net = []
        for i in range(self.num_layers):
            net.append(BN(int(m)))
            net.append(self.nonlin)
            # TODO: implement cleaner init
            net.append(nn.Linear(m, m, bias=(i==self.num_layers-1)))
            nn.init.kaiming_uniform_(net[-1].weight)
        if self.num_layers == 0:
            net.append(BN(int(m)))
            net.append(self.nonlin)
        return nn.Sequential(*net)

    def forward(self, features, **kwargs):
        output = {}
        for k, v in features.items():
            # Compute the norms and normalized features
            # v shape: [...,m , 2*k+1]
            norm = v.norm(2, -1, keepdim=True).clamp_min(self.eps).expand_as(v)
            phase = v / norm

            # Transform on norms
            transformed = self.transform[str(k)](norm[...,0]).unsqueeze(-1)

            # Nonlinearity on norm
            output[k] = (transformed * phase).view(*v.shape)

        return output


class BN(nn.Module):
    """SE(3)-equvariant batch/layer normalization"""
    def __init__(self, m):
        """SE(3)-equvariant batch/layer normalization

        Args:
            m: int for number of output channels
        """
        super().__init__()
        self.bn = nn.LayerNorm(m)

    def forward(self, x):
        return self.bn(x)


class GConvSE3Partial(nn.Module):
    """Graph SE(3)-equivariant node -> edge layer"""
    def __init__(self, f_in, f_out, edge_dim: int=0, x_ij=None):
        """SE(3)-equivariant partial convolution.

        A partial convolution computes the inner product between a kernel and
        each input channel, without summing over the result from each input
        channel. This unfolded structure makes it amenable to be used for
        computing the value-embeddings of the attention mechanism.

        Args:
            f_in: list of tuples [(multiplicities, type),...]
            f_out: list of tuples [(multiplicities, type),...]
        """
        super().__init__()
        self.f_out = f_out
        self.edge_dim = edge_dim

        # adding/concatinating relative position to feature vectors
        # 'cat' concatenates relative position & existing feature vector
        # 'add' adds it, but only if multiplicity > 1
        assert x_ij in [None, 'cat', 'add']
        self.x_ij = x_ij
        if x_ij == 'cat':
            self.f_in = Fiber.combine(f_in, Fiber(structure=[(1,1)]))
        else:
            self.f_in = f_in

        # Node -> edge weights
        self.kernel_unary = nn.ModuleDict()
        for (mi, di) in self.f_in.structure:
            for (mo, do) in self.f_out.structure:
                # 是对边的信息做更新了
                self.kernel_unary[f'({di},{do})'] = PairwiseConv(di, mi, do, mo, edge_dim=edge_dim)

    def __repr__(self):
        return f'GConvSE3Partial(structure={self.f_out})'

    def udf_u_mul_e(self, d_out):
        """Compute the partial convolution for a single output feature type.

        This function is set up as a User Defined Function in DGL.

        Args:
            d_out: output feature type
        Returns:
            node -> edge function handle
        """
        def fnc(edges):
            # Neighbor -> center messages
            # print("debug", edges.src.keys(), edges.dst.keys())
            msg = 0
            for m_in, d_in in self.f_in.structure:
                # if type 1 and flag set, add relative position as feature
                if self.x_ij == 'cat' and d_in == 1:
                    # relative positions
                    rel = (edges.dst['x'] - edges.src['x']).view(-1, 3, 1)
                    m_ori = m_in - 1
                    if m_ori == 0:
                        # no type 1 input feature, just use relative position
                        src = rel
                    else:
                        # features of src node, shape [edges, m_in*(2l+1), 1]
                        src = edges.src[f'{d_in}'].view(-1, m_ori*(2*d_in+1), 1)
                        # add to feature vector
                        src = torch.cat([src, rel], dim=1)
                elif self.x_ij == 'add' and d_in == 1 and m_in > 1:
                    src = edges.src[f'{d_in}'].view(-1, m_in*(2*d_in+1), 1)
                    rel = (edges.dst['x'] - edges.src['x']).view(-1, 3, 1)
                    src[..., :3, :1] = src[..., :3, :1] + rel
                else:
                    src = edges.src[f'{d_in}'].view(-1, m_in*(2*d_in+1), 1)  # 运行在这里
                edge = edges.data[f'({d_in},{d_out})']
                msg = msg + torch.matmul(edge, src)  # 边和节点的乘积，不过边edge的信息是已经经过basis更新过的，使用的是kernel_unary
            msg = msg.view(msg.shape[0], -1, 2*d_out+1)

            return {f'out{d_out}': msg.view(msg.shape[0], -1, 2*d_out+1)}
        return fnc

    def forward(self, h, G=None, r=None, basis=None, **kwargs):
        """Forward pass of the linear layer

        Args:
            h: dict of node-features 目前存的是0： 1：
            G: minibatch of (homo)graphs
            r: inter-atomic distances
            basis: pre-computed Q * Y
        Returns:
            tensor with new features [B, n_points, n_features_out]
        """
        with G.local_scope(): # 修改不会对原图生效
            # Add node features to local graph scope
            for k, v in h.items():
                G.ndata[k] = v

            # Add edge features
            if 'w' in G.edata.keys():
                # w 在这里存储的是pair的特征, 
                # r 存储的是两两之间的距离信息
                # feat 是边特征
                w = G.edata['w'] # shape: [#edges_in_batch, #bond_types]
                feat = torch.cat([w, r], -1)
            else:
                feat = torch.cat([r, ], -1)
            # print("GConvSE3Partial", w.shape, r.shape)
            # print("debug", self.f_in.structure, self.f_out.structure)
            # print("debug feat", feat.shape, basis.keys())
            for (mi, di) in self.f_in.structure:
                for (mo, do) in self.f_out.structure:
                    etype = f'({di},{do})'  # 有种入度 出度的感觉
                    # 感觉是使用basis对feat进行更新的意思?
                    # feat存储的是两两边的特征,这里的边是经过筛选后的边,不是L * L,
                    # kernel_unary 是一个PairwiseConv 信息
                    # 感觉是二元操作符， 然后输入feat和basis
                    # basis  是一个字典,这个应该是根据度来的， dict_keys(['0,0', '0,1', '0,2', '1,0', '1,1', '1,2', '2,0', '2,1', '2,2'])
                    # 同时 basis是基于3d转换计算了点东西,感觉是为了权重更新
                    # feat  是边特征
                    # 感觉是把边特征进行了更新
                    G.edata[etype] = self.kernel_unary[etype](feat, basis)

            # Perform message-passing for each output feature type
            # 做边信息的更新
            # 使用的是edata[etype]和点特征
            for d in self.f_out.degrees:
                G.apply_edges(self.udf_u_mul_e(d))

            # 确实，这边输出的是边信息
            # for d in self.f_out.degrees:
            #     print("GConvSE3Partial", G.edata[f'out{d}'].shape)

            return {f'{d}': G.edata[f'out{d}'] for d in self.f_out.degrees}


class GMABSE3(nn.Module):
    """An SE(3)-equivariant multi-headed self-attention module for DGL graphs."""
    def __init__(self, f_value: Fiber, f_key: Fiber, n_heads: int):
        """SE(3)-equivariant MAB (multi-headed attention block) layer.

        Args:
            f_value: Fiber() object for value-embeddings
            f_key: Fiber() object for key-embeddings
            n_heads: number of heads
        """
        super().__init__()
        self.f_value = f_value
        self.f_key = f_key
        self.n_heads = n_heads
        self.new_dgl = version.parse(dgl.__version__) > version.parse('0.4.4')

    def __repr__(self):
        return f'GMABSE3(n_heads={self.n_heads}, structure={self.f_value})'

    def udf_u_mul_e(self, d_out):
        """Compute the weighted sum for a single output feature type.

        This function is set up as a User Defined Function in DGL.

        Args:
            d_out: output feature type
        Returns:
            edge -> node function handle
        """
        def fnc(edges):
            # Neighbor -> center messages
            attn = edges.data['a']
            value = edges.data[f'v{d_out}']

            # Apply attention weights
            msg = attn.unsqueeze(-1).unsqueeze(-1) * value

            return {'m': msg}
        return fnc

    def forward(self, v, k: Dict=None, q: Dict=None, G=None, **kwargs):
        """Forward pass of the linear layer

        Args:
            G: minibatch of (homo)graphs
            v: dict of value edge-features
            k: dict of key edge-features
            q: dict of query node-features
        Returns:
            tensor with new features [B, n_points, n_features_out]
        """
        with G.local_scope():
            # Add node features to local graph scope
            ## We use the stacked tensor representation for attention
            for m, d in self.f_value.structure:
                G.edata[f'v{d}'] = v[f'{d}'].view(-1, self.n_heads, m//self.n_heads, 2*d+1)
            G.edata['k'] = fiber2head(k, self.n_heads, self.f_key, squeeze=True) # [edges, heads, channels](?)
            G.ndata['q'] = fiber2head(q, self.n_heads, self.f_key, squeeze=True) # [nodes, heads, channels](?)

            # Compute attention weights
            ## Inner product between (key) neighborhood and (query) center
            G.apply_edges(fn.e_dot_v('k', 'q', 'e'))

            ## Apply softmax
            e = G.edata.pop('e')
            if self.new_dgl:
                # in dgl 5.3, e has an extra dimension compared to dgl 4.3
                # the following, we get rid of this be reshaping
                n_edges = G.edata['k'].shape[0]
                e = e.view([n_edges, self.n_heads])
            e = e / np.sqrt(self.f_key.n_features)
            G.edata['a'] = edge_softmax(G, e)

            # Perform attention-weighted message-passing
            for d in self.f_value.degrees:
                G.update_all(self.udf_u_mul_e(d), fn.sum('m', f'out{d}'))

            output = {}
            for m, d in self.f_value.structure:
                output[f'{d}'] = G.ndata[f'out{d}'].view(-1, m, 2*d+1)

            return output


class GSE3Res(nn.Module):
    """Graph attention block with SE(3)-equivariance and skip connection"""
    def __init__(self, f_in: Fiber, f_out: Fiber, edge_dim: int=0, div: float=4,
                 n_heads: int=1, learnable_skip=True, skip='cat', selfint='1x1', x_ij=None):
        super().__init__()
        self.f_in = f_in
        self.f_out = f_out
        self.div = div
        self.n_heads = n_heads
        self.skip = skip  # valid: 'cat', 'sum', None

        # f_mid_out has same structure as 'f_out' but #channels divided by 'div'
        # this will be used for the values
        f_mid_out = {k: int(v // div) for k, v in self.f_out.structure_dict.items()}
        self.f_mid_out = Fiber(dictionary=f_mid_out)

        # f_mid_in has same structure as f_mid_out, but only degrees which are in f_in
        # this will be used for keys and queries
        # (queries are merely projected, hence degrees have to match input)
        f_mid_in = {d: m for d, m in f_mid_out.items() if d in self.f_in.degrees}
        self.f_mid_in = Fiber(dictionary=f_mid_in)

        self.edge_dim = edge_dim

        self.GMAB = nn.ModuleDict()

        # Projections
        self.GMAB['v'] = GConvSE3Partial(f_in, self.f_mid_out, edge_dim=edge_dim, x_ij=x_ij) # 用图对边的信息进行更新
        self.GMAB['k'] = GConvSE3Partial(f_in, self.f_mid_in, edge_dim=edge_dim, x_ij=x_ij)
        self.GMAB['q'] = G1x1SE3(f_in, self.f_mid_in) # 这个做的比较简单， 

        # Attention
        self.GMAB['attn'] = GMABSE3(self.f_mid_out, self.f_mid_in, n_heads=n_heads) # 这个比较复杂

        # Skip connections
        if self.skip == 'cat':
            self.cat = GCat(self.f_mid_out, f_in)
            if selfint == 'att':
                self.project = GAttentiveSelfInt(self.cat.f_out, f_out)
            elif selfint == '1x1':
                self.project = G1x1SE3(self.cat.f_out, f_out, learnable=learnable_skip)
        elif self.skip == 'sum':
            self.project = G1x1SE3(self.f_mid_out, f_out, learnable=learnable_skip)
            self.add = GSum(f_out, f_in)
            # the following checks whether the skip connection would change
            # the output fibre strucure; the reason can be that the input has
            # more channels than the ouput (for at least one degree); this would
            # then cause a (hard to debug) error in the next layer
            assert self.add.f_out.structure_dict == f_out.structure_dict, \
                'skip connection would change output structure'

    def forward(self, features, G, **kwargs):
        """
        kwargs 里边是 r 和basis, 感觉是参考信息，不变化
        features 目前存的是0：， 1：
        """
        # Embeddings
        # GConvSE3Partial 只是单纯的为了做embedding. 
        v = self.GMAB['v'](features, G=G, **kwargs) # 边 torch.Size([17696, 4, 1/3])
        k = self.GMAB['k'](features, G=G, **kwargs) # 边 torch.Size([17696, 4, 1/3])
        q = self.GMAB['q'](features, G=G) # 这里边G没有用到 点信息 torch.Size([L, 4, 1/3])

        # Attention
        z = self.GMAB['attn'](v, k=k, q=q, G=G) # 点信息 torch.Size([L, 4, 1/3])
        # for i in q:
        #     print("attention", i, q[i].shape, k[i].shape, v[i].shape, z[i].shape)

        # 有种把坐标旋转之后， 在对特征进行更新的感觉，
        # features 是点特征
        if self.skip == 'cat':
            z = self.cat(z, features)
            z = self.project(z)

        elif self.skip == 'sum':
            # Skip + residual
            z = self.project(z)
            z = self.add(z, features)
        return z

### Helper and wrapper functions

class GSum(nn.Module):
    """SE(3)-equvariant graph residual sum function."""
    def __init__(self, f_x: Fiber, f_y: Fiber):
        """SE(3)-equvariant graph residual sum function.

        Args:
            f_x: Fiber() object for fiber of summands
            f_y: Fiber() object for fiber of summands
        """
        super().__init__()
        self.f_x = f_x
        self.f_y = f_y
        self.f_out = Fiber.combine_max(f_x, f_y)

    def __repr__(self):
        return f"GSum(structure={self.f_out})"

    def forward(self, x, y):
        out = {}
        for k in self.f_out.degrees:
            k = str(k)
            if (k in x) and (k in y):
                if x[k].shape[1] > y[k].shape[1]:
                    diff = x[k].shape[1] - y[k].shape[1]
                    zeros = torch.zeros(x[k].shape[0], diff, x[k].shape[2]).to(y[k].device)
                    y[k] = torch.cat([y[k], zeros], 1)
                elif x[k].shape[1] < y[k].shape[1]:
                    diff = y[k].shape[1] - x[k].shape[1]
                    zeros = torch.zeros(x[k].shape[0], diff, x[k].shape[2]).to(y[k].device)
                    x[k] = torch.cat([x[k], zeros], 1)

                out[k] = x[k] + y[k]
            elif k in x:
                out[k] = x[k]
            elif k in y:
                out[k] = y[k]
        return out


class GCat(nn.Module):
    """Concat only degrees which are in f_x"""
    def __init__(self, f_x: Fiber, f_y: Fiber):
        super().__init__()
        self.f_x = f_x
        self.f_y = f_y
        f_out = {}
        for k in f_x.degrees:
            f_out[k] = f_x.dict[k]
            if k in f_y.degrees:
                f_out[k] = f_out[k] + f_y.dict[k]
        self.f_out = Fiber(dictionary=f_out)

    def __repr__(self):
        return f"GCat(structure={self.f_out})"

    def forward(self, x, y):
        out = {}
        for k in self.f_out.degrees:
            k = str(k)
            if k in y:
                out[k] = torch.cat([x[k], y[k]], 1)
            else:
                out[k] = x[k]
        return out


class GAvgPooling(nn.Module):
    """Graph Average Pooling module."""
    def __init__(self, type='0'):
        super().__init__()
        self.pool = AvgPooling()
        self.type = type

    def forward(self, features, G, **kwargs):
        if self.type == '0':
            h = features['0'][...,-1]
            pooled = self.pool(G, h)
        elif self.type == '1':
            pooled = []
            for i in range(3):
                h_i = features['1'][..., i]
                pooled.append(self.pool(G, h_i).unsqueeze(-1))
            pooled = torch.cat(pooled, axis=-1)
            pooled = {'1': pooled}
        else:
            print('GAvgPooling for type > 0 not implemented')
            exit()
        return pooled


class GMaxPooling(nn.Module):
    """Graph Max Pooling module."""
    def __init__(self):
        super().__init__()
        self.pool = MaxPooling()

    def forward(self, features, G, **kwargs):
        h = features['0'][...,-1]
        return self.pool(G, h)


