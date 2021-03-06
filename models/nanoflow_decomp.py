from torch import nn
from modules import Wavenet2D, Conv2D, ZeroConv2d
from torch.distributions.normal import Normal
from functions import *

#################################################################################
# experimental model 2
# based on shared estimator, decompose projection layer and allocate to multiple flows

class WaveNet2DDensityEstimator(nn.Module):
    def __init__(self, in_channel, cin_channel,
                 filter_size=256, num_layer=6, num_height=None,
                 layers_per_dilation_h_cycle=3):
        super().__init__()
        assert num_height is not None
        self.num_height = num_height
        self.layers_per_dilation_h_cycle = layers_per_dilation_h_cycle
        # dilation for width & height generation loop
        self.dilation_h = []
        self.dilation_w = []
        self.kernel_size = 3
        for i in range(num_layer):
            self.dilation_h.append(2 ** (i % self.layers_per_dilation_h_cycle))
            self.dilation_w.append(2 ** i)

        self.net = Wavenet2D(in_channels=in_channel, out_channels=filter_size,
                             num_layers=num_layer, residual_channels=filter_size,
                             gate_channels=filter_size, skip_channels=filter_size,
                             kernel_size=3, cin_channels=cin_channel,
                             dilation_h=self.dilation_h, dilation_w=self.dilation_w)

    def forward(self, x, c=None, context=None, debug=False):
        out = self.net(x, c)
        return out

    def reverse(self, x, c=None, context=None, debug=False):
        out = self.net.reverse(x, c)
        return out

    def reverse_fast(self, x, c=None, context=None, debug=False):
        out = self.net.reverse_fast(x, c)
        return out


class FlowWithSharedEstimator(nn.Module):
    def __init__(self, in_channel, cin_channel, filter_size, num_layer, num_height, layers_per_dilation_h_cycle,
                 coupling_type, num_bin, tail_bound):
        super().__init__()
        # # not used
        self.in_channel = in_channel
        # self.cin_channel = cin_channel
        # self.num_layer = num_layer
        # self.layers_per_dilation_h_cycle = layers_per_dilation_h_cycle

        self.num_height = num_height
        self.filter_size = filter_size

        self.coupling_type = coupling_type
        if self.coupling_type == 'affine':
            # projector for log_s and t
            self.proj_log_s_t = ZeroConv2d(filter_size, 2*in_channel)
        elif self.coupling_type == 'nsf':
            self.num_bin = num_bin
            self.tail_bound = tail_bound
            # projector
            self.proj = Conv2D(filter_size, self.num_bin * 3 - 1, kernel_size=1)
        else:
            raise ValueError("unknown coupling_type")

    def forward(self, estimator, x, c=None, context=None, i=None, debug=False):
        logdet = 0

        x = reverse_order(x)
        c = reverse_order(c)

        x_shift = shift_1d(x)

        feat = estimator(x_shift, c, None)

        if self.coupling_type == 'affine':
            log_s_t = self.proj_log_s_t(feat)
            log_s = log_s_t[:, :self.in_channel]
            t = log_s_t[:, self.in_channel:]
            out, logdet_af = apply_affine_coupling_forward(x, log_s, t)
            logdet = logdet + logdet_af
        elif self.coupling_type == 'nsf':
            feat = self.proj(feat)
            out, logdet_spl = apply_rq_spline_forward(x, feat, self.num_bin, self.tail_bound, self.filter_size)
            logdet = logdet + logdet_spl

        if debug:
            return out, c, logdet, log_s, t
        else:
            return out, c, logdet, None, None


    def reverse_opt(self, estimator, z, c=None, context=None):
        x = torch.zeros_like(z[:, :, 0:1, :])

        for i_h in range(self.num_height):
            feat = estimator.reverse(x, c[:, :, :, :i_h + 1, :], None)
            feat = feat[:, :, -1, :].unsqueeze(2)

            if self.coupling_type == 'affine':
                log_s_t = self.proj_log_s_t(feat)
                log_s = log_s_t[:, :self.in_channel]
                t = log_s_t[:, self.in_channel:]
                x_new = apply_affine_coupling_inverse(z[:, :, i_h, :].unsqueeze(2), log_s, t)
            elif self.coupling_type == 'nsf':
                feat = self.proj(feat)
                x_new = apply_rq_spline_inverse(z[:, :, i_h, :].unsqueeze(2), feat, self.num_bin, self.tail_bound,
                                                self.filter_size)
            x_new = x_new.unsqueeze(2)
            x = torch.cat((x, x_new), 2)

        x = x[:, :, 1:, :]

        x = reverse_order(x)
        c = reverse_order(c, dim=3)  # height dim is 3 for cached c

        return x, c

    def reverse_fast(self, estimator, z, c=None, context=None):
        x = torch.zeros_like(z[:, :, 0:1, :])
        estimator.net.conv_queue_init(x)

        for i_h in range(self.num_height):
            feat = estimator.reverse_fast(x if i_h == 0 else x_new,
                                          c[:, :, :, i_h:i_h + 1, :], None)
            feat = feat[:, :, -1, :].unsqueeze(2)

            if self.coupling_type == 'affine':
                log_s_t = self.proj_log_s_t(feat)
                log_s = log_s_t[:, :self.in_channel]
                t = log_s_t[:, self.in_channel:]
                x_new = apply_affine_coupling_inverse(z[:, :, i_h, :].unsqueeze(2), log_s, t)
            elif self.coupling_type == 'nsf':
                feat = self.proj(feat)
                x_new = apply_rq_spline_inverse(z[:, :, i_h, :].unsqueeze(2), feat, self.num_bin, self.tail_bound,
                                                self.filter_size)
            x_new = x_new.unsqueeze(2)
            x = torch.cat((x, x_new), 2)

        x = x[:, :, 1:, :]

        x = reverse_order(x)
        c = reverse_order(c, dim=3)  # height dim is 3 for cached c

        return x, c


class NanoFlowDecomp(nn.Module):
    def __init__(self, in_channel, cin_channel, res_channel, n_height, n_flow, n_layer, layers_per_dilation_h_cycle,
                 coupling_type, num_bin=32, tail_bound=5.0):
        super().__init__()
        self.in_channel = in_channel
        self.cin_channel = cin_channel
        self.res_channel = res_channel
        self.n_height = n_height
        self.n_flow = n_flow
        self.n_layer = n_layer

        self.layers_per_dilation_h_cycle = layers_per_dilation_h_cycle

        self.coupling_type = coupling_type
        if self.coupling_type == 'affine':
            pass
        elif self.coupling_type == 'nsf':
            # nsf params
            self.num_bin = num_bin
            self.tail_bound = tail_bound
        else:
            raise ValueError("unknown coupling type")

        # major change: shared WaveNet2D weight
        self.estimator = WaveNet2DDensityEstimator(self.in_channel, self.cin_channel,
                                                   self.res_channel, self.n_layer, self.n_height,
                                                   self.layers_per_dilation_h_cycle)
        self.flows = nn.ModuleList()
        for i in range(self.n_flow):
            self.flows.append(
                FlowWithSharedEstimator(self.in_channel, self.cin_channel, filter_size=self.res_channel,
                                        num_layer=self.n_layer, num_height=self.n_height,
                                        layers_per_dilation_h_cycle=self.layers_per_dilation_h_cycle,
                                        coupling_type=self.coupling_type,
                                        num_bin=num_bin, tail_bound=tail_bound))

        self.upsample_conv = nn.ModuleList()
        for s in [16, 16]:
            convt = nn.ConvTranspose2d(1, 1, (3, 2 * s), padding=(1, s // 2), stride=(1, s))
            convt = nn.utils.weight_norm(convt)
            nn.init.kaiming_normal_(convt.weight)
            self.upsample_conv.append(convt)
            self.upsample_conv.append(nn.LeakyReLU(0.4))

        self.upsample_conv_kernel_size = (2*s)**2
        self.upsample_conv_stride = s**2

    def forward(self, x, c, debug=False):
        x = x.unsqueeze(1)
        B, _, T = x.size()
        #  Upsample spectrogram to size of audio
        c = self.upsample(c)
        assert(c.size(2) >= x.size(2))
        if c.size(2) > x.size(2):
            c = c[:, :, :x.size(2)]

        x, c = squeeze_to_2d(x, c, h=self.n_height)
        out = x

        logdet = 0

        if debug:
            list_log_s, list_t  = [], []

        for i, flow in enumerate(self.flows):
            out, c, logdet_new, log_s, t = flow(self.estimator, out, c, None, i, debug)

            if debug:
                list_log_s.append(log_s)
                list_t.append(t)

            logdet = logdet + logdet_new

        if debug:
            return out, logdet, list_log_s, list_t
        else:
            return out, logdet

    def reverse(self, c, temp=1.0, debug_z=None):
        c = self.upsample(c)
        # trim conv artifacts. maybe pad spec to kernel multiple
        time_cutoff = self.upsample_conv_kernel_size - self.upsample_conv_stride
        c = c[:, :, :-time_cutoff]

        B, _, T_c = c.size()

        _, c = squeeze_to_2d(None, c, h=self.n_height)

        if debug_z is None:
            # sample gaussian noise that matches c
            q_0 = Normal(c.new_zeros((B, 1, c.size()[2], c.size()[3])), c.new_ones((B, 1, c.size()[2], c.size()[3])))
            z = q_0.sample() * temp
        else:
            z = debug_z

        # pre-compute conditioning tensors and cache them
        c_cache = []
        for i, resblock in enumerate(self.estimator.net.res_blocks):
            filter_gate_conv_c = resblock.filter_gate_conv_c(c)
            c_cache.append(filter_gate_conv_c)
        c_cache = torch.stack(c_cache)  # [num_layers, batch_size, res_channels, width, height]

        for i, flow in enumerate(self.flows[::-1]):
            z, c_cache = flow.reverse_opt(self.estimator, z, c_cache, None)

        x = unsqueeze_to_1d(z, self.n_height)

        return x

    def reverse_fast(self, c, temp=1.0, debug_z=None):
        c = self.upsample(c)
        # trim conv artifacts. maybe pad spec to kernel multiple
        time_cutoff = self.upsample_conv_kernel_size - self.upsample_conv_stride
        c = c[:, :, :-time_cutoff]

        B, _, T_c = c.size()

        _, c = squeeze_to_2d(None, c, h=self.n_height)

        if debug_z is None:
            # sample gaussian noise that matches c
            q_0 = Normal(c.new_zeros((B, 1, c.size()[2], c.size()[3])), c.new_ones((B, 1, c.size()[2], c.size()[3])))
            z = q_0.sample() * temp
        else:
            z = debug_z

        # pre-compute conditioning tensors and cache them
        c_cache =self.estimator.net.fused_filter_gate_conv_c(c)
        c_cache = c_cache.reshape(c_cache.shape[0], self.n_layer, self.res_channel*2, c_cache.shape[2], c_cache.shape[3])
        c_cache = c_cache.permute(1, 0, 2, 3, 4) # [num_layers, batch_size, res_channels, height, width]
        c_cache_reversed = reverse_order(c_cache, dim=3)

        for i, flow in enumerate(self.flows[::-1]):
            c_cache_i = c_cache if i % 2 == 0 else c_cache_reversed
            z, _ = flow.reverse_fast(self.estimator, z, c_cache_i)

        x = unsqueeze_to_1d(z, self.n_height)

        return x

    def upsample(self, c):
        c = c.unsqueeze(1)
        for f in self.upsample_conv:
            c = f(c)
        c = c.squeeze(1)
        return c

    def remove_weight_norm(self):
        # remove weight norm from all weights
        for layer in self.upsample_conv.children():
            try:
                torch.nn.utils.remove_weight_norm(layer)
            except ValueError:
                pass

        net = self.estimator.net
        torch.nn.utils.remove_weight_norm(net.front_conv[0].conv)
        for resblock in net.res_blocks.children():
            torch.nn.utils.remove_weight_norm(resblock.filter_gate_conv.conv)
            torch.nn.utils.remove_weight_norm(resblock.filter_gate_conv_c)
            if hasattr(resblock, "filter_gate_conv_h"):
                torch.nn.utils.remove_weight_norm(resblock.filter_gate_conv_h)
            torch.nn.utils.remove_weight_norm(resblock.res_skip_conv)

        total_params = sum(p.numel() for p in self.parameters() if p.requires_grad)
        print("weight_norm removed: {} params".format(total_params))

    def fuse_conditioning_layers(self):
        # fuse mel-spec conditioning layers into one big conv weight
        net = self.estimator.net
        cin_channels = net.res_blocks[0].cin_channels
        out_channels = net.res_blocks[0].out_channels
        fused_filter_gate_conv_c = nn.Conv2d(cin_channels, 2*out_channels*self.n_layer, kernel_size=1)
        fused_filter_gate_conv_c_weight = []
        fused_filter_gate_conv_c_bias = []
        for resblock in net.res_blocks.children():
            fused_filter_gate_conv_c_weight.append(resblock.filter_gate_conv_c.weight)
            fused_filter_gate_conv_c_bias.append(resblock.filter_gate_conv_c.bias)
            del resblock.filter_gate_conv_c

        fused_filter_gate_conv_c.weight = torch.nn.Parameter(torch.cat(fused_filter_gate_conv_c_weight).clone())
        fused_filter_gate_conv_c.bias = torch.nn.Parameter(torch.cat(fused_filter_gate_conv_c_bias).clone())
        self.estimator.net.fused_filter_gate_conv_c = fused_filter_gate_conv_c

        print("INFO: conditioning layers fused for performance: only reverse_fast function can be used for inference!")
        total_params = sum(p.numel() for p in self.parameters() if p.requires_grad)
        print("model after optimization: {} params".format(total_params))

# end of experimental model 2
#################################################################################
if __name__ == "__main__":
    x = torch.randn((2, 15872)).cuda()
    c = torch.randn((2, 80, 62)).cuda()

    net = NanoFlowDecomp(1, 80, 64, 8, 4, 8, 1, 'affine', 8, 5.).cuda()
    out = net(x, c)

    with torch.no_grad():
        out = net.reverse(c)
        # remove all weight_norm from the model
        net.remove_weight_norm()
        # fuse mel-spec conditioning layer weights to maximize speed
        net.fuse_conditioning_layers()
        out = net.reverse_fast(c)
