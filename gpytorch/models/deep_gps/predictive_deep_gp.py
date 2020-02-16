import math
from itertools import product

import numpy as np
import torch
from numpy.polynomial.hermite import hermgauss

from gpytorch import settings
from gpytorch.distributions import MultitaskMultivariateNormal
from gpytorch.lazy import BlockDiagLazyTensor
from gpytorch.likelihoods import GaussianLikelihood, MultitaskGaussianLikelihood, MultitaskGaussianLikelihoodKronecker

from ..approximate_gp import ApproximateGP
from ..gp import GP
from .deep_gp import DeepGPLayer as AbstractDeepGPLayer


class _DeepGPVariationalStrategy(object):
    def __init__(self, model):
        self.model = model

    @property
    def sub_variational_strategies(self):
        if not hasattr(self, "_sub_variational_strategies_memo"):
            self._sub_variational_strategies_memo = [
                module.variational_strategy for module in self.model.modules() if isinstance(module, ApproximateGP)
            ]
        return self._sub_variational_strategies_memo

    def kl_divergence(self):
        return sum(strategy.kl_divergence().sum() for strategy in self.sub_variational_strategies)


class AbstractPredictiveDeepGPLayer(AbstractDeepGPLayer):
    def __init__(self, variational_strategy, input_dims, output_dims, num_sample_sites=3, grid_strategy="flipgrid", xi=None, sample_site_dims=None):
        super().__init__(variational_strategy, input_dims, output_dims)

        assert grid_strategy in ['flipgrid', 'freegrid', 'freeform']
        self.grid_strategy = grid_strategy

        self.num_sample_sites = num_sample_sites

        if sample_site_dims is None:
            self.sample_site_dims = input_dims
        else:
            self.sample_site_dims = sample_site_dims

        # Pass in previous_layer.xi if you want to share xi across layers.
        if xi is not None:
            self.xi = xi
        else:
            # quad_grid is of size Q^T x T
            #if output_dims is None: this hack was here because only the topmost layer needed these things
            if grid_strategy == 'freegrid':
                xi, _ = hermgauss(self.num_sample_sites)
                self.xi = torch.nn.Parameter(torch.from_numpy(xi).float().unsqueeze(-1).repeat(1, self.sample_site_dims))
            elif grid_strategy == 'flipgrid':
                xi, _ = hermgauss(self.num_sample_sites)
                self.xi = torch.nn.Parameter(0.5 * torch.from_numpy(xi).float().unsqueeze(-1).repeat(1, self.sample_site_dims))
            elif grid_strategy == 'freeform':
                self.xi = torch.nn.Parameter(torch.randn(num_sample_sites, self.sample_site_dims))

    @property
    def quad_grid(self):
        if self.grid_strategy == 'flipgrid':
            xi = self.xi - self.xi.flip(dims=[0])
            res = torch.stack([torch.cat([p.unsqueeze(-1) for p in xi]) for xi in product(*xi.t())])
            assert res.size(-2) == math.pow(self.num_sample_sites, self.self.sample_site_dims) and res.size(-1) == self.self.sample_site_dims
            return res
        elif self.grid_strategy == 'freegrid':
            xi = self.xi
            res = torch.stack([torch.cat([p.unsqueeze(-1) for p in xi]) for xi in product(*xi.t())])
            assert res.size(-2) == math.pow(self.num_sample_sites, self.self.sample_site_dims) and res.size(-1) == self.self.sample_site_dims
            return res
        elif self.grid_strategy == 'freeform':
            return self.xi

    def __call__(self, inputs, *other_inputs, are_samples=False, expand_for_quadgrid=True, **kwargs):
        if isinstance(inputs, MultitaskMultivariateNormal):
            # inputs is definitely in the second layer, and mean is n x t
            mus, sigmas = inputs.mean, inputs.variance.sqrt()

            # HACK: could also do explicit shape checking, but I think this will work for multitask too.
            if expand_for_quadgrid:
                xi_mus = mus.unsqueeze(-3)  # 1 x n x t
                xi_sigmas = sigmas.unsqueeze(-3)  # 1 x n x t
            else:
                xi_mus = mus
                xi_sigmas = sigmas

            # unsqueeze sigmas to 1 x n x t, locations from [q] to Q^T x 1 x T.
            # Broadcasted result will be Q^T x N x T
            qg = self.quad_grid.unsqueeze(-2)
            # qg = qg + torch.randn_like(qg) * 1e-2
            xi_sigmas = xi_sigmas * qg

            inputs = xi_mus + xi_sigmas  # q^t x n x t

        if len(other_inputs):
            processed_inputs = [
                inp.unsqueeze(0).expand(self.num_sample_sites, *inp.shape)
                for inp in other_inputs
            ]

            inputs = torch.cat([inputs] + processed_inputs, dim=-1)

        if settings.debug.on():
            if not torch.is_tensor(inputs):
                raise ValueError(
                    "`inputs` should either be a MultitaskMultivariateNormal or a Tensor, got "
                    f"{inputs.__class__.__Name__}"
                )

            if inputs.size(-1) != self.input_dims:
                raise RuntimeError(
                    f"Input shape did not match self.input_dims. Got total feature dims [{inputs.size(-1)}],"
                    f" expected [{self.input_dims}]"
                )

        # Repeat the input for all possible outputs
        if self.output_dims is not None:
            inputs = inputs.unsqueeze(-3)
            inputs = inputs.expand(*inputs.shape[:-3], self.output_dims, *inputs.shape[-2:])
        # Now run samples through the GP
        output = ApproximateGP.__call__(self, inputs, **kwargs)

        if self.num_sample_sites > 0:
            if self.output_dims is not None and not isinstance(output, MultitaskMultivariateNormal):
                mean = output.loc.transpose(-1, -2)
                covar = BlockDiagLazyTensor(output.lazy_covariance_matrix, block_dim=-3)
                output = MultitaskMultivariateNormal(mean, covar, interleaved=False)
        else:
            output = output.loc.transpose(-1, -2)  # this layer provides noiseless kernel interpolation

        return output


class AbstractDeepGP(GP):
    def __init__(self):
        """
        A container module to build a DeepGP.
        This module should contain `AbstractDeepGPLayer` modules, and can also contain other modules as well.
        """
        super().__init__()
        self.variational_strategy = _DeepGPVariationalStrategy(self)

    def forward(self, x, **kwargs):
        raise NotImplementedError


class DeepPredictiveGaussianLikelihood(GaussianLikelihood):
    def __init__(self, dims, num_sample_sites=3, grid_strategy="flipgrid", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_sample_sites = num_sample_sites

        if grid_strategy in ['flipgrid', 'freegrid']:
            _, weights = hermgauss(self.num_sample_sites)

            # Q^T x T
            self.register_parameter("raw_quad_weight_grid", torch.nn.Parameter(
                    torch.stack([torch.tensor(a) for a in product(*[np.log(weights) for _ in range(dims)])])),)

            # QT
            self.raw_quad_weight_grid.data = self.raw_quad_weight_grid.data.sum(dim=-1)
        elif grid_strategy == 'freeform':
            self.register_parameter("raw_quad_weight_grid", torch.nn.Parameter(torch.randn(self.num_sample_sites)))

    @property
    def quad_weight_grid(self):
        qwd = self.raw_quad_weight_grid
        return qwd - qwd.logsumexp(dim=-1)

    def log_marginal(self, observations, function_dist, *params, **kwargs):
        # Q^T x N
        base_log_marginal = super().log_marginal(observations, function_dist)
        deep_log_marginal = self.quad_weight_grid.unsqueeze(-1) + base_log_marginal

        deep_log_prob = deep_log_marginal.logsumexp(dim=-2)

        return deep_log_prob

    def forward(self, *args, **kwargs):
        pass

class MultitaskDeepPredictiveGaussianLikelihood(MultitaskGaussianLikelihood):
    def __init__(self, dims, num_sample_sites=3, grid_strategy="flipgrid", *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_sample_sites = num_sample_sites

        if grid_strategy in ['flipgrid', 'freegrid']:
            _, weights = hermgauss(self.num_sample_sites)

            # Q^T x T
            self.register_parameter("raw_quad_weight_grid", torch.nn.Parameter(
                    torch.stack([torch.tensor(a) for a in product(*[np.log(weights) for _ in range(dims)])])),)

            # QT
            self.raw_quad_weight_grid.data = self.raw_quad_weight_grid.data.sum(dim=-1)
        elif grid_strategy == 'freeform':
            self.register_parameter("raw_quad_weight_grid", torch.nn.Parameter(torch.randn(self.num_sample_sites)))

    @property
    def quad_weight_grid(self):
        qwd = self.raw_quad_weight_grid
        return qwd - qwd.logsumexp(dim=-1)

    def log_marginal(self, observations, function_dist, *params, **kwargs):
        # Q^T x N
        base_log_marginal = super().log_marginal(observations, function_dist)
        deep_log_marginal = self.quad_weight_grid.unsqueeze(-1) + base_log_marginal

        deep_log_prob = deep_log_marginal.logsumexp(dim=-2)

        return deep_log_prob

    def forward(self, *args, **kwargs):
        pass