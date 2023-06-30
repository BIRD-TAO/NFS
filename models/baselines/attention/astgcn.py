import math
from typing import Optional, List, Union

import torch
import torch.nn as nn
from torch.nn import Parameter
import torch.nn.functional as F

from torch_geometric.data import Data
from torch_geometric.typing import OptTensor
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.transforms import LaplacianLambdaMax
from torch_geometric.utils import remove_self_loops, add_self_loops, get_laplacian


class ChebConvAttention(MessagePassing):
    r"""The chebyshev spectral graph convolutional operator with attention from the
    `Attention Based Spatial-Temporal Graph Convolutional 
    Networks for Traffic Flow Forecasting." <https://ojs.aaai.org/index.php/AAAI/article/view/3881>`_ paper
    :math:`\mathbf{\hat{L}}` denotes the scaled and normalized Laplacian
    :math:`\frac{2\mathbf{L}}{\lambda_{\max}} - \mathbf{I}`.
    
    Args:
        in_channels (int): Size of each input sample.
        out_channels (int): Size of each output sample.
        K (int): Chebyshev filter size :math:`K`.
        normalization (str, optional): The normalization scheme for the graph
            Laplacian (default: :obj:`"sym"`):
            1. :obj:`None`: No normalization
            :math:`\mathbf{L} = \mathbf{D} - \mathbf{A}`
            2. :obj:`"sym"`: Symmetric normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1/2} \mathbf{A}
            \mathbf{D}^{-1/2}`
            3. :obj:`"rw"`: Random-walk normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1} \mathbf{A}`
            You need to pass :obj:`lambda_max` to the :meth:`forward` method of
            this operator in case the normalization is non-symmetric.
            :obj:`\lambda_max` should be a :class:`torch.Tensor` of size
            :obj:`[num_graphs]` in a mini-batch scenario and a
            scalar/zero-dimensional tensor when operating on single graphs.
            You can pre-compute :obj:`lambda_max` via the
            :class:`torch_geometric.transforms.LaplacianLambdaMax` transform.
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    def __init__(self, in_channels: int, out_channels: int, K: int, normalization: Optional[str]=None,
                 bias: bool=True, **kwargs):
        kwargs.setdefault('aggr', 'add')
        super(ChebConvAttention, self).__init__(**kwargs)

        assert K > 0
        assert normalization in [None, 'sym', 'rw'], 'Invalid normalization'

        self._in_channels = in_channels #1
        self._out_channels = out_channels #64
        self._normalization = normalization #None
        self._weight = Parameter(torch.Tensor(K, in_channels, out_channels)) # torch.Size([1, 1, 64])

        if bias:
            self._bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('_bias', None)

        self._reset_parameters()

    def _reset_parameters(self):
        nn.init.xavier_uniform_(self._weight)
        if self._bias is not None:
            nn.init.uniform_(self._bias)

    def __norm__(self, edge_index, num_nodes: Optional[int],
                 edge_weight: OptTensor, normalization: Optional[str],
                 lambda_max, dtype: Optional[int] = None,
                 batch: OptTensor = None):

        edge_index, edge_weight = remove_self_loops(edge_index, edge_weight)

        edge_index, edge_weight = get_laplacian(edge_index, edge_weight,
                                                normalization, dtype,
                                                num_nodes)

        if batch is not None and lambda_max.numel() > 1:
            lambda_max = lambda_max[batch[edge_index[0]]]
##?
        edge_weight = (2.0 * edge_weight) / lambda_max
        edge_weight.masked_fill_(edge_weight == float('inf'), 0)

        edge_index, edge_weight = add_self_loops(edge_index, edge_weight,
                                                 fill_value=-1.,
                                                 num_nodes=num_nodes)
        assert edge_weight is not None

        return edge_index, edge_weight

    def forward(self, x: torch.FloatTensor, edge_index: torch.LongTensor,
                spatial_attention: torch.FloatTensor, edge_weight: OptTensor = None,
                batch: OptTensor = None, lambda_max: OptTensor = None) -> torch.FloatTensor:
        """
        Making a forward pass of the ChebConv Attention layer.
        
        Arg types:
            * x (PyTorch Float Tensor) - Node features for T time periods, with shape (B, N_nodes, F_in).
            * edge_index (Tensor array) - Edge indices.
            * spatial_attention (PyTorch Float Tensor) - Spatial attention weights, with shape (B, N_nodes, N_nodes).
            * edge_weight (PyTorch Float Tensor, optional) - Edge weights corresponding to edge indices.
            * batch (PyTorch Tensor, optional) - Batch labels for each edge.
            * lambda_max (optional, but mandatory if normalization is None) - Largest eigenvalue of Laplacian.

        Return types:
            * out (PyTorch Float Tensor) - Hidden state tensor for all nodes, with shape (B, N_nodes, F_out).
        """
        if self._normalization != 'sym' and lambda_max is None:
            raise ValueError('You need to pass `lambda_max` to `forward() in`'
                             'case the normalization is non-symmetric.')
##  X torch.Size([32, 2048, 1])
        if lambda_max is None:
            lambda_max = torch.tensor(2.0, dtype=x.dtype, device=x.device)
        if not isinstance(lambda_max, torch.Tensor):
            lambda_max = torch.tensor(lambda_max, dtype=x.dtype,
                                      device=x.device)
        assert lambda_max is not None

        edge_index, norm = self.__norm__(edge_index, x.size(self.node_dim),
                                         edge_weight, self._normalization,
                                         lambda_max, dtype=x.dtype,
                                         batch=batch) ## norm edge_weight
        row, col = edge_index
        Att_norm = norm * spatial_attention[:,row,col] #torch.Size([32, 53248])
        num_nodes = x.size(self.node_dim)
        TAx_0 = torch.matmul((torch.eye(num_nodes).to(edge_index.device)*spatial_attention).permute(0,2,1),x) #####? 
        out = torch.matmul(TAx_0, self._weight[0])
        edge_index_transpose = edge_index[[1,0]]
        if self._weight.size(0) > 1:
            TAx_1 = self.propagate(edge_index_transpose, x=TAx_0, norm=Att_norm, size=None)
            out = out + torch.matmul(TAx_1, self._weight[1])

        for k in range(2, self._weight.size(0)):
            TAx_2 = self.propagate(edge_index_transpose, x=TAx_1, norm=norm, size=None)
            TAx_2 = 2. * TAx_2 - TAx_0
            out = out + torch.matmul(TAx_2, self._weight[k])
            TAx_0, TAx_1 = TAx_1, TAx_2

        if self._bias is not None:
            out += self._bias

        return out

    def message(self, x_j, norm):
        if norm.dim() == 1:
            return norm.view(-1, 1) * x_j
        else:
            d1, d2 = norm.shape
            return norm.view(d1,d2, 1) * x_j

    def __repr__(self):
        return '{}({}, {}, K={}, normalization={})'.format(
            self.__class__.__name__, self._in_channels, self._out_channels,
            self._weight.size(0), self._normalization)


class SpatialAttention(nn.Module):
    r"""An implementation of the Spatial Attention Module. For details see this paper: 
    `"Attention Based Spatial-Temporal Graph Convolutional Networks for Traffic Flow 
    Forecasting." <https://ojs.aaai.org/index.php/AAAI/article/view/3881>`_

    Args:
        in_channels (int): Number of input features.
        num_of_vertices (int): Number of vertices in the graph.
        num_of_timesteps (int): Number of time lags.
    """
    def __init__(self, in_channels: int, num_of_vertices: int, num_of_timesteps: int):
        super(SpatialAttention, self).__init__()
        
        self._W1 = nn.Parameter(torch.FloatTensor(num_of_timesteps)) #12
        self._W2 = nn.Parameter(torch.FloatTensor(in_channels, num_of_timesteps)) #torch.Size([1, 12])
        self._W3 = nn.Parameter(torch.FloatTensor(in_channels)) # torch.Size([1])
        self._bs = nn.Parameter(torch.FloatTensor(1, num_of_vertices, num_of_vertices)) #torch.Size([1, 2048, 2048])
        self._Vs = nn.Parameter(torch.FloatTensor(num_of_vertices, num_of_vertices)) # torch.Size([2048, 2048])
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
            else:
                nn.init.uniform_(p)
     

    def forward(self, X: torch.FloatTensor) -> torch.FloatTensor:
        """
        Making a forward pass of the spatial attention layer.
        
        Arg types:
            * **X** (PyTorch FloatTensor) - Node features for T time periods, with shape (B, N_nodes, F_in, T_in).

        Return types:
            * **S** (PyTorch FloatTensor) - Spatial attention score matrices, with shape (B, N_nodes, N_nodes).
        """

        LHS = torch.matmul(torch.matmul(X, self._W1), self._W2) # X torch.Size([32, 2048, 1, 12])   self._W1: torch.Size([12]) torch.matmul(X, self._W1) torch.Size([32, 2048, 1])  W2 torch.Size([1, 12]) ->> torch.Size([32, 2048, 12])
        RHS = torch.matmul(self._W3, X).transpose(-1, -2) # torch.Size([1]) torch.Size([32, 2048, 1, 12])  ->torch.Size([32, 2048, 12])  ->torch.Size([32, 12, 2048])
        S = torch.matmul(self._Vs, torch.sigmoid(torch.matmul(LHS, RHS) + self._bs)) #torch.Size([2048, 2048])  torch.matmul(LHS, RHS) torch.Size([32, 2048, 2048]) torch.Size([1, 2048, 2048])
        S = F.softmax(S, dim=1) # torch.Size([32, 2048, 2048])
        return S



class TemporalAttention(nn.Module):
    r"""An implementation of the Temporal Attention Module. For details see this paper: 
    `"Attention Based Spatial-Temporal Graph Convolutional Networks for Traffic Flow 
    Forecasting." <https://ojs.aaai.org/index.php/AAAI/article/view/3881>`_

    Args:
        in_channels (int): Number of input features.
        num_of_vertices (int): Number of vertices in the graph.
        num_of_timesteps (int): Number of time lags.
    """
    def __init__(self, in_channels: int, num_of_vertices: int, num_of_timesteps: int):
        super(TemporalAttention, self).__init__()
        
        self._U1 = nn.Parameter(torch.FloatTensor(num_of_vertices)) #2048
        self._U2 = nn.Parameter(torch.FloatTensor(in_channels, num_of_vertices)) #torch.Size([1, 2048])
        self._U3 = nn.Parameter(torch.FloatTensor(in_channels)) #torch.Size([1])
        self._be = nn.Parameter(torch.FloatTensor(1, num_of_timesteps, num_of_timesteps)) #torch.Size([1, 12, 12])
        self._Ve = nn.Parameter(torch.FloatTensor(num_of_timesteps, num_of_timesteps)) #torch.Size([12, 12])
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
            else:
                nn.init.uniform_(p)

    def forward(self, X: torch.FloatTensor) -> torch.FloatTensor:
        """
        Making a forward pass of the temporal attention layer.
       
        Arg types:
            * **X** (PyTorch FloatTensor) - Node features for T time periods, with shape (B, N_nodes, F_in, T_in).

        Return types:
            * **E** (PyTorch FloatTensor) - Temporal attention score matrices, with shape (B, T_in, T_in).
        """##X.T.shape torch.Size([12, 1, 2048, 32]) self._U1  torch.Size([2048])
        LHS = torch.matmul(torch.matmul(X.permute(0, 3, 2, 1), self._U1), self._U2) # X torch.Size([32, 2048, 1, 12]) ->X.permute(0, 3, 2, 1).shape torch.Size([32, 12, 1, 2048]) matmul   torch.matmul(X.permute(0, 3, 2, 1), self._U1).shape->torch.Size([32, 12, 1])  U2:torch.Size([1, 2048])  torch.Size([32, 12, 2048])
        RHS = torch.matmul(self._U3, X) #   torch.Size([1])  torch.Size([32, 2048, 1, 12])    torch.Size([32, 2048, 12])
        E = torch.matmul(self._Ve, torch.sigmoid(torch.matmul(LHS, RHS) + self._be)) #   torch.Size([32, 12, 12]) *  torch.Size([1, 12, 12])    torch.Size([32, 12, 12])
        E = F.softmax(E, dim=1)
        return E

class ASTGCNBlock(nn.Module):
    r"""An implementation of the Attention Based Spatial-Temporal Graph Convolutional Block.
    For details see this paper: `"Attention Based Spatial-Temporal Graph Convolutional 
    Networks for Traffic Flow Forecasting." <https://ojs.aaai.org/index.php/AAAI/article/view/3881>`_

    Args:
        in_channels (int): Number of input features.
        K (int): Order of Chebyshev polynomials. Degree is K-1.
        nb_chev_filter (int): Number of Chebyshev filters.
        nb_time_filter (int): Number of time filters.
        time_strides (int): Time strides during temporal convolution.
        num_of_vertices (int): Number of vertices in the graph.
        num_of_timesteps (int): Number of time lags.
        normalization (str, optional): The normalization scheme for the graph
            Laplacian (default: :obj:`"sym"`):
            1. :obj:`None`: No normalization
            :math:`\mathbf{L} = \mathbf{D} - \mathbf{A}`
            2. :obj:`"sym"`: Symmetric normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1/2} \mathbf{A}
            \mathbf{D}^{-1/2}`
            3. :obj:`"rw"`: Random-walk normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1} \mathbf{A}`
            You need to pass :obj:`lambda_max` to the :meth:`forward` method of
            this operator in case the normalization is non-symmetric.
            :obj:`\lambda_max` should be a :class:`torch.Tensor` of size
            :obj:`[num_graphs]` in a mini-batch scenario and a
            scalar/zero-dimensional tensor when operating on single graphs.
            You can pre-compute :obj:`lambda_max` via the
            :class:`torch_geometric.transforms.LaplacianLambdaMax` transform.
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
    """
    def __init__(self, in_channels: int, K: int, nb_chev_filter: int, nb_time_filter: int,
                 time_strides: int, num_of_vertices: int, num_of_timesteps: int, 
                 normalization: Optional[str]=None,bias: bool=True):
        super(ASTGCNBlock, self).__init__()
        
        self._temporal_attention = TemporalAttention(in_channels, num_of_vertices, num_of_timesteps)
        self._spatial_attention = SpatialAttention(in_channels, num_of_vertices, num_of_timesteps)
        self._chebconv_attention = ChebConvAttention(in_channels, nb_chev_filter, K, normalization, bias)
        self._time_convolution = nn.Conv2d(nb_chev_filter, nb_time_filter, kernel_size=(1, 3), stride=(1, time_strides), padding=(0, 1))
        self._residual_convolution = nn.Conv2d(in_channels, nb_time_filter, kernel_size=(1, 1), stride=(1, time_strides))
        self._layer_norm = nn.LayerNorm(nb_time_filter)
        self._normalization = normalization
        
        self._reset_parameters()

    def _reset_parameters(self):
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
            else:
                nn.init.uniform_(p)

    def forward(self, X: torch.FloatTensor, edge_index: Union[torch.LongTensor,List[torch.LongTensor]]) -> torch.FloatTensor:
        """
        Making a forward pass with the ASTGCN block.
 
        Arg types:
            * **X** (PyTorch Float Tensor) - Node features for T time periods, with shape (B, N_nodes, F_in, T_in).
            * **edge_index** (LongTensor): Edge indices, can be an array of a list of Tensor arrays, depending on whether edges change over time.

        Return types:
            * **X** (PyTorch Float Tensor) - Hidden state tensor for all nodes, with shape (B, N_nodes, nb_time_filter, T_out).
        """
        batch_size, num_of_vertices, num_of_features, num_of_timesteps = X.shape

        X_tilde = self._temporal_attention(X)# torch.Size([32, 12, 12]) ##? 下一步为什么要变维度
        X_tilde = torch.matmul(X.reshape(batch_size, -1, num_of_timesteps), X_tilde)  # X.reshape(batch_size, -1, num_of_timesteps) torch.Size([32, 2048, 12]) X_tilde:torch.Size([32, 12, 12])  ->torch.Size([32, 2048, 12])
        X_tilde = X_tilde.reshape(batch_size, num_of_vertices, num_of_features, num_of_timesteps) # torch.Size([32, 2048, 1, 12])
        X_tilde = self._spatial_attention(X_tilde)
 # torch.Size([32, 2048, 2048])
        if not isinstance(edge_index, list):
            data = Data(edge_index=edge_index, edge_attr=None, num_nodes=num_of_vertices) # Data(edge_index=[2, 51200])
            if self._normalization != 'sym':
                lambda_max = LaplacianLambdaMax()(data).lambda_max
            else:
                lambda_max = None
            X_hat = [] ## convolution in temporal dimension
            for t in range(num_of_timesteps): #12  torch.Size([32, 2048, 1, 12])
                X_hat.append(torch.unsqueeze(self._chebconv_attention(X[:,:,:,t], edge_index, X_tilde, lambda_max=lambda_max), -1))
    
            X_hat = F.relu(torch.cat(X_hat, dim=-1))       
        else:
            X_hat = []
            for t in range(num_of_timesteps):
                data = Data(edge_index=edge_index[t], edge_attr=None, num_nodes=num_of_vertices)
                if self._normalization != 'sym':
                    lambda_max = LaplacianLambdaMax()(data).lambda_max
                else:
                    lambda_max = None
                X_hat.append(torch.unsqueeze(self._chebconv_attention(X[:,:,:,t], edge_index[t], X_tilde, lambda_max=lambda_max), -1))
            X_hat = F.relu(torch.cat(X_hat, dim=-1))

        X_hat = self._time_convolution(X_hat.permute(0, 2, 1, 3)) # x_hat torch.Size([32, 2048, 64, 12]) ->torch.Size([32, 64, 2048, 2])
        X = self._residual_convolution(X.permute(0, 2, 1, 3)) # torch.Size([32, 2048, 1, 12]) -> torch.Size([32, 64, 2048, 2])
        X = self._layer_norm(F.relu(X + X_hat).permute(0, 3, 2, 1)) 
        X = X.permute(0, 2, 3, 1) # torch.Size([32, 2, 2048, 64]) -> torch.Size([32, 2048, 64, 2])
        return X


class ASTGCN(nn.Module):
    r"""An implementation of the Attention Based Spatial-Temporal Graph Convolutional Cell.
    For details see this paper: `"Attention Based Spatial-Temporal Graph Convolutional 
    Networks for Traffic Flow Forecasting." <https://ojs.aaai.org/index.php/AAAI/article/view/3881>`_

    Args:
        nb_block (int): Number of ASTGCN blocks in the model.
        in_channels (int): Number of input features.
        K (int): Order of Chebyshev polynomials. Degree is K-1.
        nb_chev_filters (int): Number of Chebyshev filters.
        nb_time_filters (int): Number of time filters.
        time_strides (int): Time strides during temporal convolution.
        edge_index (array): edge indices.
        num_for_predict (int): Number of predictions to make in the future.
        len_input (int): Length of the input sequence.
        num_of_vertices (int): Number of vertices in the graph.
        normalization (str, optional): The normalization scheme for the graph
            Laplacian (default: :obj:`"sym"`):
            1. :obj:`None`: No normalization
            :math:`\mathbf{L} = \mathbf{D} - \mathbf{A}`
            2. :obj:`"sym"`: Symmetric normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1/2} \mathbf{A}
            \mathbf{D}^{-1/2}`
            3. :obj:`"rw"`: Random-walk normalization
            :math:`\mathbf{L} = \mathbf{I} - \mathbf{D}^{-1} \mathbf{A}`
            You need to pass :obj:`lambda_max` to the :meth:`forward` method of
            this operator in case the normalization is non-symmetric.
            :obj:`\lambda_max` should be a :class:`torch.Tensor` of size
            :obj:`[num_graphs]` in a mini-batch scenario and a
            scalar/zero-dimensional tensor when operating on single graphs.
            You can pre-compute :obj:`lambda_max` via the
            :class:`torch_geometric.transforms.LaplacianLambdaMax` transform.
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
    """
    def __init__(self, nb_block: int, input_size: int, output_size: int, max_view: int, nb_chev_filter: int, nb_time_filter: int,
                 time_strides: int, pred_len: int, seq_len: int, num_of_nodes: int, 
                 normalization: Optional[str]=None, bias: bool=True, **model_kwargs):

        super(ASTGCN, self).__init__()
        self.pred_len = pred_len #12
        self.output_size = output_size #1
        self._blocklist = nn.ModuleList([ASTGCNBlock(input_size, max_view, nb_chev_filter, nb_time_filter,
                                         time_strides, num_of_nodes, seq_len, normalization, bias)])

        self._blocklist.extend([ASTGCNBlock(nb_time_filter, max_view, nb_chev_filter, nb_time_filter, 1, 
                        num_of_nodes, seq_len//time_strides, normalization, bias) for _ in range(nb_block-1)])

        self._final_conv = nn.Conv2d(int(seq_len/time_strides), pred_len*output_size, kernel_size=(1, nb_time_filter))

        self._reset_parameters()

    def _reset_parameters(self):
        """
        Resetting the parameters.
        """
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
            else:
                nn.init.uniform_(p)

    def forward(self, X: torch.FloatTensor, edge_index: torch.LongTensor, *args) -> torch.FloatTensor:
        """
        Making a forward pass.
        
        Arg types:
            * **X** (PyTorch FloatTensor) - Node features for T time periods, with shape (B, N_nodes, F_in, T_in).
            * **edge_index** (PyTorch LongTensor): Edge indices, can be an array of a list of Tensor arrays, depending on whether edges change over time.

        Return types:
            * **X** (PyTorch FloatTensor)* - Hidden state tensor for all nodes, with shape (B, N_nodes, T_out).
        """
        B, N_nodes, F_in, T_in = X.shape
        F_out = self.output_size
        T_out = self.pred_len #12

        for block in self._blocklist:
            X = block(X, edge_index)
# torch.Size([32, 2048, 64, 2])
        X = self._final_conv(X.permute(0, 3, 1, 2)) # torch.Size([32, 12, 2048, 1])
        X = X[:, :, :, -1]  # torch.Size([32, 12, 2048])
        X = X.reshape(B, T_out, F_out, N_nodes) # X :X.reshape(B, T_out, F_out, N_nodes) torch.Size([32, 12, 1, 2048])
        X = X.permute(1, 0, 3, 2)
        return X # torch.Size([12, 32, 2048, 1])


