import math
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import ChebConv

class TemporalConv(nn.Module):
    r"""Temporal convolution block applied to nodes in the STGCN Layer
    For details see: `"Spatio-Temporal Graph Convolutional Networks:
    A Deep Learning Framework for Traffic Forecasting." 
    <https://arxiv.org/abs/1709.04875>`_ Based off the temporal convolution
     introduced in "Convolutional Sequence to Sequence Learning"  <https://arxiv.org/abs/1709.04875>`_

    Args:
        in_channels (int): Number of input features.
        out_channels (int): Number of output features.
        kernel_sizeatt (int): Convolutional kernel size.
    """
    def __init__(self, in_channels: int, out_channels: int, kernel_sizeatt: int=3):
        super(TemporalConv, self).__init__()
        self.conv_1 = nn.Conv2d(in_channels, out_channels, (1, kernel_sizeatt))
        self.conv_2 = nn.Conv2d(in_channels, out_channels, (1, kernel_sizeatt))
        self.conv_3 = nn.Conv2d(in_channels, out_channels, (1, kernel_sizeatt))

    def forward(self, X: torch.FloatTensor) -> torch.FloatTensor:
        """Forward pass through temporal convolution block.
        
        Arg types:
            * **X** (torch.FloatTensor) -  Input data of shape 
                (batch_size, input_time_steps, num_nodes, in_channels).

        Return types:
            * **H** (torch.FloatTensor) - Output data of shape 
                (batch_size, in_channels, num_nodes, input_time_steps).
        """
        X = X.permute(0, 3, 2, 1)#b,d,n,t
        P = self.conv_1(X)
        Q = torch.sigmoid(self.conv_2(X)) #b,d,n,t
        PQ = P * Q #b,d,n,t
        H = F.relu(PQ + self.conv_3(X))
        H = H.permute(0, 3, 2, 1)
        return H

class STGCN(nn.Module):
    r"""Spatio-temporal convolution block using ChebConv Graph Convolutions. 
    For details see: `"Spatio-Temporal Graph Convolutional Networks:
    A Deep Learning Framework for Traffic Forecasting" 
    <https://arxiv.org/abs/1709.04875>`_

    NB. The ST-Conv block contains two temporal convolutions (TemporalConv) 
    with kernel size k. Hence for an input sequence of length m, 
    the output sequence will be length m-2(k-1).

    Args:
        in_channels (int): Number of input features.
        hidden_channels (int): Number of hidden units output by graph convolution block
        out_channels (int): Number of output features.
        kernel_sizeatt (int): Size of the kernel considered. 
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

    """
    def __init__(self, num_of_nodes: int, input_size: int, hidden_units: int, 
                output_size: int, kernel_sizeatt: int, max_view: int, pred_len: int,
                normalization: str="sym", bias: bool=True, **model_kwargs):
        super(STGCN, self).__init__()
        self.num_nodes = num_of_nodes
        self.input_size = input_size
        self.hidden_units = hidden_units
        self.out_channels = output_size
        self.kernel_sizeatt = kernel_sizeatt
        self.K = max_view
        self.normalization = normalization
        self.bias = bias

        self._temporal_conv1 = TemporalConv(in_channels=input_size, 
                                            out_channels=hidden_units, 
                                            kernel_sizeatt=kernel_sizeatt)
                                        
        self._graph_conv = ChebConv(in_channels=hidden_units,
                                   out_channels=hidden_units,
                                   K=max_view,
                                   normalization=normalization,
                                   bias=bias)
                                
        self._temporal_conv2 = TemporalConv(in_channels=hidden_units, 
                                            out_channels=output_size * pred_len, 
                                            kernel_sizeatt=kernel_sizeatt)
                                        
        self._batch_norm = nn.BatchNorm2d(num_of_nodes)
        
    def forward(self, X: torch.FloatTensor, edge_index: torch.LongTensor,
                edge_weight: torch.FloatTensor=None, **kwargs) -> torch.FloatTensor:
                
        r"""Forward pass. If edge weights are not present the forward pass
        defaults to an unweighted graph. 

        Arg types:
            * **X** (PyTorch FloatTensor) - Sequence of node features of shape (Batch size X Input time steps X Num nodes X In channels).
            * **edge_index** (PyTorch LongTensor) - Graph edge indices.
            * **edge_weight** (PyTorch LongTensor, optional)- Edge weight vector.
        
        Return types:
            * **T** (PyTorch FloatTensor) - Sequence of node features.
        """
        X = X.permute(0, 3, 1, 2) #b,n,d,t->b,t,n,d
        batch_size, seq_len, num_of_nodes, input_size = X.shape
        T_0 = self._temporal_conv1(X)
        T = torch.zeros_like(T_0).to(T_0.device)
        for b in range(T_0.size(0)):
            for t in range(T_0.size(1)):
                T[b][t] = self._graph_conv(T_0[b][t], edge_index, edge_weight)

        T = F.relu(T)#torch.Size([8, 7, 2048, 64]) b,t,n,d
        T = self._temporal_conv2(T)
        T = T.permute(0, 2, 1, 3)
        T = self._batch_norm(T)
        T = T.permute(3, 0, 1, 2)[..., -1].reshape(seq_len, batch_size, num_of_nodes, -1)
        return T
