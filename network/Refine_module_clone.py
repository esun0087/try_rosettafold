import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
from Transformer import LayerNorm
from InitStrGenerator import make_graph
from InitStrGenerator import get_seqsep, UniMPBlock
from Attention_module_w_str import make_graph as make_graph_topk
from Attention_module_w_str import get_bonded_neigh, rbf
from SE3_network import SE3Transformer
from Transformer import _get_clones, create_custom_forward
# Re-generate initial coordinates based on 1) final pair features 2) predicted distogram
# Then, refine it through multiple SE3 transformer block

class Regen_Network(nn.Module):
    def __init__(self, 
                 node_dim_in=64, 
                 node_dim_hidden=64,
                 edge_dim_in=128, 
                 edge_dim_hidden=64, 
                 state_dim=8,
                 nheads=4, 
                 nblocks=3, 
                 dropout=0.0):
        super(Regen_Network, self).__init__()

        # embedding layers for node and edge features
        self.norm_node = LayerNorm(node_dim_in)
        self.norm_edge = LayerNorm(edge_dim_in)

        self.embed_x = nn.Sequential(nn.Linear(node_dim_in+21, node_dim_hidden), LayerNorm(node_dim_hidden))
        self.embed_e = nn.Sequential(nn.Linear(edge_dim_in+2, edge_dim_hidden), LayerNorm(edge_dim_hidden))
        
        # graph transformer
        blocks = [UniMPBlock(node_dim_hidden,edge_dim_hidden,nheads,dropout) for _ in range(nblocks)]
        self.transformer = nn.Sequential(*blocks)
        
        # outputs
        self.get_xyz = nn.Linear(node_dim_hidden,9)
        self.norm_state = LayerNorm(node_dim_hidden)
        self.get_state = nn.Linear(node_dim_hidden, state_dim)
    
    def forward(self, seq1hot, idx, node, edge):
        B, L = node.shape[:2]
        node = self.norm_node(node)
        edge = self.norm_edge(edge)
        
        node = torch.cat((node, seq1hot), dim=-1)
        node = self.embed_x(node)

        seqsep = get_seqsep(idx) 
        neighbor = get_bonded_neigh(idx)
        edge = torch.cat((edge, seqsep, neighbor), dim=-1)
        edge = self.embed_e(edge)
        
        G = make_graph(node, idx, edge)
        Gout = self.transformer(G)
        
        xyz = self.get_xyz(Gout.x)
        state = self.get_state(self.norm_state(Gout.x))
        return xyz.reshape(B, L, 3, 3) , state.reshape(B, L, -1)

class Refine_Network(nn.Module):
    def __init__(self, d_node=64, d_pair=128, d_state=16,
            SE3_param={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32}, p_drop=0.0):
        super(Refine_Network, self).__init__()
        self.norm_msa = LayerNorm(d_node)
        self.norm_pair = LayerNorm(d_pair)
        self.norm_state = LayerNorm(d_state)

        self.embed_x = nn.Linear(d_node+21+d_state, SE3_param['l0_in_features'])
        self.embed_e1 = nn.Linear(d_pair, SE3_param['num_edge_features'])
        self.embed_e2 = nn.Linear(SE3_param['num_edge_features']+36+1, SE3_param['num_edge_features'])
        
        self.norm_node = LayerNorm(SE3_param['l0_in_features'])
        self.norm_edge1 = LayerNorm(SE3_param['num_edge_features'])
        self.norm_edge2 = LayerNorm(SE3_param['num_edge_features'])
        
        self.se3 = SE3Transformer(**SE3_param)

    @torch.cuda.amp.autocast(enabled=True)
    def forward(self, msa, pair, xyz, state, seq1hot, idx, top_k=64):
        # process node & pair features
        B, L = msa.shape[:2]
        node = self.norm_msa(msa)
        pair = self.norm_pair(pair)
        state = self.norm_state(state)
       
        node = torch.cat((node, seq1hot, state), dim=-1)
        node = self.norm_node(self.embed_x(node))
        pair = self.norm_edge1(self.embed_e1(pair))
        
        neighbor = get_bonded_neigh(idx) # 下标中的位置
        rbf_feat = rbf(torch.cdist(xyz[:,:,1,:], xyz[:,:,1,:]))
        pair = torch.cat((pair, rbf_feat, neighbor), dim=-1)
        pair = self.norm_edge2(self.embed_e2(pair))
        
        # define graph
        G = make_graph_topk(xyz, pair, idx, top_k=top_k)
        l1_feats = xyz - xyz[:,:,1,:].unsqueeze(2) # l1 features = displacement vector to CA
        l1_feats = l1_feats.reshape(B*L, -1, 3)
        
        # apply SE(3) Transformer & update coordinates
        shift = self.se3(G, node.reshape(B*L, -1, 1), l1_feats)

        state = shift['0'].reshape(B, L, -1) # (B, L, C)
        
        offset = shift['1'].reshape(B, L, -1, 3) # (B, L, 3, 3)
        CA_new = xyz[:,:,1] + offset[:,:,1]
        N_new = CA_new + offset[:,:,0]
        C_new = CA_new + offset[:,:,2]
        xyz_new = torch.stack([N_new, CA_new, C_new], dim=2)

        return xyz_new, state

class Refine_module(nn.Module):
    def __init__(self, n_module, d_node=64, d_node_hidden=64, d_pair=128, d_pair_hidden=64,
                 SE3_param={'l0_in_features':32, 'l0_out_features':16, 'num_edge_features':32}, p_drop=0.0):
        super(Refine_module, self).__init__()
        self.n_module = n_module
        self.proj_edge = nn.Linear(d_pair, d_pair_hidden*2)

        self.regen_net = Regen_Network(node_dim_in=d_node, node_dim_hidden=d_node_hidden,
                                       edge_dim_in=d_pair_hidden*2, edge_dim_hidden=d_pair_hidden,
                                       state_dim=SE3_param['l0_out_features'],
                                       nheads=4, nblocks=3, dropout=p_drop)
        self.refine_net = _get_clones(Refine_Network(d_node=d_node, d_pair=d_pair_hidden*2,
                                         d_state=SE3_param['l0_out_features'],
                                         SE3_param=SE3_param, p_drop=p_drop), self.n_module)
        self.norm_state = LayerNorm(SE3_param['l0_out_features'])
        
        self.pred_lddt = nn.Sequential(nn.Linear(SE3_param['l0_out_features'], 1), nn.Sigmoid())

    def forward(self, node, edge, seq1hot, idx):
        edge = self.proj_edge(edge)

        xyz, state = self.regen_net(seq1hot, idx, node, edge) # 用node和edge，经过图网络生成坐标信息
        # print(f"region net xyz {xyz.shape} state {state.shape} input seq1hot {seq1hot.shape} idx {idx.shape} edge {edge.shape}", )
       
        # for test train
        for i_m in range(self.n_module):
            xyz, state = checkpoint.checkpoint(create_custom_forward(self.refine_net[i_m], top_k=64), node.float(), edge.float(), xyz.float(), state.float(), seq1hot, idx)
        #
        lddt = self.pred_lddt(self.norm_state(state)) 
        return xyz, lddt
