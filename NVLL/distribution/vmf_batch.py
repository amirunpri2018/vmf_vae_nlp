import numpy as np
import torch
from scipy import special as sp
import time
from NVLL.util.util import GVar


class vMF_fast(torch.nn.Module):
    def __init__(self, hid_dim, lat_dim, kappa=1):
        super().__init__()
        self.hid_dim = hid_dim
        self.lat_dim = lat_dim
        self.kappa = kappa
        # self.func_kappa = torch.nn.Linear(hid_dim, lat_dim)
        self.func_mu = torch.nn.Linear(hid_dim, lat_dim)

        self.kld = GVar(torch.from_numpy(vMF_fast._vmf_kld(kappa, lat_dim)).float())
        print('KLD: {}'.format(self.kld.data[0]))

    def estimate_param(self, latent_code):
        ret_dict = {}
        ret_dict['kappa'] = self.kappa

        # Only compute mu, use mu/mu_norm as mu,
        #  use 1 as norm, use diff(mu_norm, 1) as redundant_norm
        mu = self.func_mu(latent_code)

        norm = torch.norm(mu, 2, 1, keepdim=True)
        mu_norm_sq_diff_from_one = torch.pow(torch.add(norm, -1), 2)
        redundant_norm = torch.sum(mu_norm_sq_diff_from_one, dim=1, keepdim=True)
        ret_dict['norm'] = torch.ones_like(mu)
        ret_dict['redundant_norm'] = redundant_norm

        mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
        ret_dict['mu'] = mu

        return ret_dict

    def compute_KLD(self, tup, batch_sz):
        return self.kld.expand(batch_sz)

    @staticmethod
    def _vmf_kld(k, d):
        tmp = (k * ((sp.iv(d / 2.0 + 1.0, k) + sp.iv(d / 2.0, k) * d / (2.0 * k)) / sp.iv(d / 2.0, k) - d / (2.0 * k)) \
               + d * np.log(k) / 2.0 - np.log(sp.iv(d / 2.0, k)) \
               - sp.loggamma(d / 2 + 1) - d * np.log(2) / 2).real
        if tmp != tmp:
            exit()
        return np.array([tmp])

    def build_bow_rep(self, lat_code, n_sample):
        batch_sz = lat_code.size()[0]
        tup = self.estimate_param(latent_code=lat_code)
        mu = tup['mu']
        norm = tup['norm']
        kappa = tup['kappa']

        kld = self.compute_KLD(tup, batch_sz)
        vecs = []
        if n_sample == 1:
            return tup, kld, self.sample_cell(mu, norm, kappa)
        for n in range(n_sample):
            sample = self.sample_cell(mu, norm, kappa)
            vecs.append(sample)
        vecs = torch.cat(vecs, dim=0)
        return tup, kld, vecs

    def sample_cell(self, mu, norm, kappa):
        batch_sz, lat_dim = mu.size()
        mu = GVar(mu)
        mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
        w = self._sample_weight_batch(kappa, lat_dim, batch_sz)
        w = w.unsqueeze(1)


        start = time.time()
        # batch version
        w_var = GVar(w * torch.ones(batch_sz, lat_dim))
        v = self._sample_ortho_batch(mu, lat_dim)
        scale_factr = torch.sqrt(
            GVar(torch.ones(batch_sz, lat_dim)) - torch.pow(w_var, 2))
        orth_term = v * scale_factr
        muscale = mu * w_var
        sampled_vec = orth_term + muscale
        mid = time.time()
        print(sampled_vec, mid - start)

        start = time.time()
        # non batch version
        sampled_vecs = GVar(torch.FloatTensor(batch_sz, lat_dim))
        for b in range(batch_sz):
            this_mu = mu[b]
            w_var = GVar(w[b] * torch.ones(lat_dim))
            v = self._sample_orthonormal_to(this_mu, lat_dim)
            scale_factr = torch.sqrt(GVar(torch.ones(lat_dim)) - torch.pow(w_var, 2))
            orth_term = v * scale_factr
            muscale = this_mu * w_var
            sv = orth_term + muscale
            sampled_vecs[b] = sv
        end  = time.time()
        print(sampled_vecs, end-start)
        return sampled_vec.unsqueeze(0)

    def _sample_weight_batch(self, kappa, dim, batch_sz=1):
        result = torch.FloatTensor((batch_sz))
        for b in range(batch_sz):
            result[b] = self._sample_weight(kappa, dim)
        return result

    def _sample_weight(self, kappa, dim):
        """Rejection sampling scheme for sampling distance from center on
        surface of the sphere.
        """
        dim = dim - 1  # since S^{n-1}
        b = dim / (np.sqrt(4. * kappa ** 2 + dim ** 2) + 2 * kappa)  # b= 1/(sqrt(4.* kdiv**2 + 1) + 2 * kdiv)
        x = (1. - b) / (1. + b)
        c = kappa * x + dim * np.log(1 - x ** 2)  # dim * (kdiv *x + np.log(1-x**2))

        while True:
            z = np.random.beta(dim / 2., dim / 2.)  # concentrates towards 0.5 as d-> inf
            w = (1. - (1. + b) * z) / (1. - (1. - b) * z)
            u = np.random.uniform(low=0, high=1)
            if kappa * w + dim * np.log(1. - x * w) - c >= np.log(
                    u):  # thresh is dim *(kdiv * (w-x) + log(1-x*w) -log(1-x**2))
                return w

    def _sample_ortho_batch(self, mu, dim):
        """

        :param mu: Variable, [batch size, latent dim]
        :param dim: scala. =latent dim
        :return:
        """
        _batch_sz, _lat_dim = mu.size()
        assert _lat_dim == dim
        squeezed_mu = mu.unsqueeze(1)

        v = GVar(torch.randn(_batch_sz, dim, 1))        #TODO random

        # v = GVar(torch.linspace(-1, 1, steps=dim))
        # v = v.expand(_batch_sz, dim).unsqueeze(2)

        rescale_val = torch.bmm(squeezed_mu, v).squeeze(2)
        proj_mu_v = mu * rescale_val
        ortho = v.squeeze() - proj_mu_v
        ortho_norm = torch.norm(ortho, p=2, dim=1, keepdim=True)
        y = ortho / ortho_norm
        return y

    def _sample_orthonormal_to(self, mu, dim):
        """Sample point on sphere orthogonal to mu.
        """
        v = GVar(torch.randn(dim))      # TODO random

        # v = GVar(torch.linspace(-1,1,steps=dim))

        rescale_value = mu.dot(v) / mu.norm()
        proj_mu_v = mu * rescale_value.expand(dim)
        ortho = v - proj_mu_v
        ortho_norm = torch.norm(ortho)
        return ortho / ortho_norm.expand_as(ortho)


vmf = vMF_fast(50, 100, 100)
batchsz = 100

mu = torch.FloatTensor(np.random.uniform(0, 1, 20 * batchsz))
mu = mu.view(batchsz, -1)
mu = mu / torch.norm(mu, p=2, dim=1, keepdim=True)
vmf.sample_cell(mu, None, 100)
