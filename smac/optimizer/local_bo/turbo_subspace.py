import typing
import math

import numpy as np
from scipy.stats.qmc import Sobol, LatinHypercube

from ConfigSpace.hyperparameters import NumericalHyperparameter
from ConfigSpace.util import deactivate_inactive_hyperparameters

from smac.configspace import Configuration, ConfigurationSpace
from smac.epm.gaussian_process import GaussianProcess
from smac.epm.gaussian_process_mcmc import GaussianProcessMCMC
from smac.epm.partial_sparse_gaussian_process import PartialSparseGaussianProcess
from smac.epm.gaussian_process_gpytorch import GaussianProcessGPyTorch
from smac.epm.base_epm import AbstractEPM
from smac.optimizer.acquisition import AbstractAcquisitionFunction
from smac.optimizer.acquisition import TS
from smac.optimizer.local_bo.abstract_subspace import AbstractSubspace


class TuRBOSubSpace(AbstractSubspace):
    def __init__(self,
                 config_space: ConfigurationSpace,
                 bounds: typing.List[typing.Tuple[float, float]],
                 hps_types: typing.List[int],
                 bounds_ss_cont: typing.Optional[np.ndarray] = None,
                 bounds_ss_cat: typing.Optional[typing.List[typing.Tuple]] = None,
                 model_local: AbstractEPM = PartialSparseGaussianProcess,
                 model_local_kwargs: typing.Optional[typing.Dict] = None,
                 acq_func_local: AbstractAcquisitionFunction = TS,
                 acq_func_local_kwargs: typing.Optional[typing.Dict] = None,
                 rng: typing.Optional[np.random.RandomState] = None,
                 initial_data: typing.Optional[typing.Tuple[np.ndarray, np.ndarray]] = None,
                 activate_dims: typing.Optional[np.ndarray] = None,
                 incumbent_array: typing.Optional[np.ndarray] = None,
                 length_init: float = 0.8,
                 length_min: float = 0.5 ** 8,
                 length_max: float = 1.6,
                 success_tol: int = 3,
                 failure_tol_min: int = 4,
                 n_init_x_params: int = 2,
                 n_candidate_max: int = 5000,
                 ):
        """
        Subspace designed for TurBO:
        D. Eriksson et al. Scalable Global Optimization via Local Bayesian Optimization
        https://arxiv.org/pdf/1910.01739.pdf
        The hyperparameters are the same as teh setting under supplementary D TuRBO details
        Parameters
        ----------
        length_init: float
            initialized length of subspace
        length_min: float
            minimal length of subspace, if subspace has length smaller than this value, turbo will restart
        length_max: float
            maximal length of subspace
        success_tol: float
           the number of successive successful evaluations required for expanding the subregion
        failure_tol_min: float
           minimal number of successive successful evaluations required for shrinking the subregion (otherwise
           this value is set as number of feature dimensions)
        n_init_x_params: int
            how many configurations will be used at most in the initial design (X*D). Used for restarting the subspace
        n_candidate_max: int
            Maximal Number of points used as candidates
        """
        super(TuRBOSubSpace, self).__init__(config_space=config_space,
                                            bounds=bounds,
                                            hps_types=hps_types,
                                            bounds_ss_cont=bounds_ss_cont,
                                            bounds_ss_cat=bounds_ss_cat,
                                            model_local=model_local,
                                            model_local_kwargs=model_local_kwargs,
                                            acq_func_local=acq_func_local,
                                            acq_func_local_kwargs=acq_func_local_kwargs,
                                            rng=rng,
                                            initial_data=initial_data,
                                            activate_dims=activate_dims,
                                            incumbent_array=incumbent_array)
        hps = config_space.get_hyperparameters()
        for hp in hps:
            if not isinstance(hp, NumericalHyperparameter):
                raise ValueError("Current TurBO Optimizer only supports Numerical Hyperparameters")
        if len(config_space.get_conditions()) > 0 or len(config_space.get_forbiddens()) > 0:
            raise ValueError("Currently TurBO does not support conditional or forbidden hyperparameters")

        n_hps = len(self.activate_dims)
        self.n_dims = n_hps
        self.n_init = n_init_x_params * self.n_dims
        self.n_candidates = min(100 * n_hps, n_candidate_max)

        self.failure_tol = max(failure_tol_min, n_hps)
        self.success_tol = success_tol
        self.length = length_init
        self.length_init = length_init
        self.length_min = length_min
        self.length_max = length_max
        self._restart_turbo(n_init_points=self.n_init)

        self.lb = np.zeros(self.n_dims)
        self.ub = np.ones(self.n_dims)
        self.config_origin = "TuRBO"

    def _restart_turbo(self,
                       n_init_points: int,
                       ):
        """
        restart TurBO with a certain number of initialized points. New points are initialized with latin hypercube

        Parameters
        ----------
        n_init_points: int
            number of points required for initializing a new subspace
        """
        self.logger.debug("Current length is smaller than the minimal value, a new TuRBO restarts")
        self.success_count = 0
        self.failure_count = 0

        self.num_eval_this_round = 0
        self.last_incumbent_value = np.inf
        self.length = self.length_init

        self.model_x = np.empty([0, self.n_dims])
        self.ss_x = np.empty([0, self.n_dims])
        self.model_y = np.empty([0, 1])
        self.ss_y = np.empty([0, 1])

        init_vectors = LatinHypercube(d=self.n_dims,
                                      seed=np.random.seed(self.rng.randint(1, 2 ** 20))).random(n=n_init_points)

        self.init_configs = [Configuration(self.cs_local, vector=init_vector) for init_vector in init_vectors]

    def adjust_length(self, new_observation: typing.Union[float, np.ndarray]):
        """
        Adjust the subspace length according to the performance of the latest suggested values
        Parameters
        ----------
        new_observation: typing.Union[float, np.ndarray]
            new observations
        """
        # see Section 2: 'Trust regions'
        optim_observation = new_observation if np.isscalar(new_observation) else np.min(new_observation)

        # We define a ``success'' as a candidate that improves upon $\xbest$, and a ``failure'' as a candidate that
        # does not.
        if optim_observation < np.min(self.model_y) - 1e-3 * math.fabs(np.min(self.model_y)):
            self.logger.debug("New suggested value is better than the incumbent, success_count increases")
            self.success_count += 1
            self.failure_count = 0
        else:
            self.logger.debug("New suggested value is worse than the incumbent, failure_count increases")
            self.success_count = 0
            self.failure_count += 1

        # After $\tau_{\text{succ}}$ consecutive successes, we double the size of the TR,
        # i.e., $\len \gets \min\{\len_{\textrm{max}}, 2\len\}$.
        if self.success_count == self.success_tol:  # Expand trust region
            self.length = min([2.0 * self.length, self.length_max])
            self.success_count = 0
            self.logger.debug(f"Subspace length expands to {self.length}")
        # After $\tau_{\text{fail}}$ consecutive failures, we halve the size of the TR: $\len \gets \len/2$.
        # We reset the success and failure counters to zero after we change the size of the TR.
        elif self.failure_count == self.failure_tol:  # Shrink trust region
            self.length /= 2.0
            self.failure_count = 0
            self.logger.debug(f"Subspace length shrinks to {self.length}")

    def _generate_challengers(self, _sorted=True):
        """
        generate new challengers list for this subspace
        """
        if len(self.init_configs) > 0:
            config_next = self.init_configs.pop()
            config_next.origin = "TurBO Init"
            return [(0, config_next)]

        if self.length < self.length_min:
            self._restart_turbo(n_init_points=self.n_init)
            config_next = self.init_configs.pop()
            config_next.origin = "TurBO Init"
            return [(0, config_next)]

        self.model.train(self.model_x, self.model_y)
        self.update_model(predict_x_best=False, update_incumbent_array=True)

        sobol_gen = Sobol(d=self.n_dims, scramble=True, seed=self.rng.randint(low=0, high=10000000))
        sobol_seq = sobol_gen.random(self.n_candidates)

        # adjust length according to kernel length
        if isinstance(self.model, (GaussianProcess, GaussianProcessMCMC,
                                   PartialSparseGaussianProcess, GaussianProcessGPyTorch)):
            if isinstance(self.model, GaussianProcess):
                kernel_length = np.exp(self.model.hypers[1:-1])
            elif isinstance(self.model, GaussianProcessMCMC):
                kernel_length = np.exp(np.mean((np.array(self.model.hypers)[:, 1:-1]), axis=0))
            elif isinstance(self.model, (GaussianProcessGPyTorch, PartialSparseGaussianProcess)):
                kernel_length = self.model.kernel.base_kernel.lengthscale.cpu().detach().numpy()

            # See section 'Trust regions' of section 2
            #  $\len_i = \lambda_i L / (\prod_{j=1}^d \lambda_j)^{1/d}$,
            kernel_length = kernel_length / kernel_length.mean()  # This will make the next line more stable
            subspace_scale = kernel_length / np.prod(
                np.power(kernel_length, 1.0 / self.n_dims))  # We now have weights.prod() = 1

            subspace_length = self.length * subspace_scale

            subspace_lb = np.clip(self.incumbent_array - subspace_length * 0.5, 0.0, 1.0)
            subspace_ub = np.clip(self.incumbent_array + subspace_length * 0.5, 0.0, 1.0)
            sobol_seq = sobol_seq * (subspace_ub - subspace_lb) + subspace_lb

            # See Supplementary D, 'TuRBO details':
            # In order to not perturb all coordinates at once, we use the value in the Sobol sequence
            # with probability min{1,20/d}for a given candidate and dimension, and the value of the center otherwise
            prob_perturb = min(20.0 / self.n_dims, 1.0)
            mask = np.random.rand(self.n_candidates, self.n_dims) <= prob_perturb
            ind = np.where(np.sum(mask, axis=1) == 0)[0]
            mask[ind, np.random.randint(0, self.n_dims - 1, size=len(ind))] = 1

            # Create candidate points
            design = self.incumbent_array * np.ones((self.n_candidates, self.n_dims))
            design[mask] = sobol_seq[mask]
        else:
            design = sobol_seq

        # Only numerical hyperpameters are considered for TuRBO, we don't need to transfer the vectors to fit the
        # requirements of other sorts of hyperparameters
        configs = []
        if _sorted:
            origin = "Sobol Sequnce (Sorted)"
        else:
            origin = "Sobol Sequence"
        for vector in design:
            conf = deactivate_inactive_hyperparameters(configuration=None,
                                                       configuration_space=self.cs_local,
                                                       vector=vector)
            conf.origin = origin
            configs.append(conf)

        if _sorted:
            return self._sort_configs_by_acq_value(configs)
        else:
            return [(0, configs[i]) for i in range(len(configs))]

    def _sort_configs_by_acq_value(
            self,
            configs: typing.List[Configuration]
    ) -> typing.List[typing.Tuple[float, Configuration]]:
        """Sort the given configurations by acquisition value
        comes from smac.optimizer.ei_optimization.AcquisitionFunctionMaximizer

        Parameters
        ----------
        configs : list(Configuration)

        Returns
        -------
        list: (acquisition value, Candidate solutions),
                ordered by their acquisition function value
        """

        acq_values = self.acquisition_function(configs)

        # From here
        # http://stackoverflow.com/questions/20197990/how-to-make-argsort-result-to-be-random-between-equal-values
        random = self.rng.rand(len(acq_values))
        # Last column is primary sort key!
        indices = np.lexsort((random.flatten(), acq_values.flatten()))

        # Cannot use zip here because the indices array cannot index the
        # rand_configs list, because the second is a pure python list
        return [(acq_values[ind][0], configs[ind]) for ind in indices[::-1]]