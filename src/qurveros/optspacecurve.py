"""
This module extends the functionality of the SpaceCurve class by providing
optimization methods for the auxiliary parameters.
The BarqCurve class is also defined in this module.
"""

import jax
import jax.numpy as jnp
import numpy
import optax
import pandas as pd
import warnings
import logging

from qurveros import beziertools, barqtools, spacecurve

from qurveros.misctools import progbar_range
from qurveros.settings import settings


class OptimizableSpaceCurve(spacecurve.SpaceCurve):

    """
    Extends the SpaceCurve class to provide optimization over the
    auxiliary parameters.

    Attributes:
        opt_loss: The optimization loss function created using the
                  prepare_optimization_loss method.
        params_history: The parameters obtained from each optimization step.
        loss_grad: The gradient of the loss function.

    Methods:
        prepare_optimization_loss: Creates the optimization loss based on
        the provided loss functions and their associated weights.
        optimize: Optimizes the curve's auxiliary parameters.
        update_params_from_opt_history: Updates the curve's parameters based
        on a chosen optimization step.

    Note:
        When an instance is indexed with a string, optimization information
        is returned. See the optimize method.
    """

    def __init__(self, *, curve, order, interval, params=None,
                 deriv_array_fun=None):

        self.opt_loss = None
        self.params_history = None
        self.loss_grad = lambda params: None

        super().__init__(curve=curve,
                         order=order,
                         interval=interval,
                         params=params,
                         deriv_array_fun=deriv_array_fun)

    def initialize_parameters(self, init_params=None):

        """
        Initializes the parameters for the optimization.
        """

        if init_params is not None:
            self.set_params(init_params)

    def prepare_optimization_loss(self, *loss_list, interval=None):

        """
        Creates the optimization loss based on a loss list.

        Args:
            loss_list(lists): The argument is a series of lists of the form
                [loss_function, weight]. The total loss is constructed as a
                linear combination of the function values multiplied by
                the respective weights.
            interval (list): The closed interval for the curve parameter where
            optimization takes place. The default value corresponds to the
            interval provided upon instantiation.
        """

        if interval is None:
            interval = self.interval

        x_values = jnp.linspace(*interval, settings.options['OPT_POINTS'])

        @jax.jit
        def opt_loss(params):

            frenet_dict = self._frenet_dict_fun(x_values, params)

            loss = 0.
            for loss_fun, weight in loss_list:
                loss = loss + weight * loss_fun(frenet_dict)

            return loss

        self.opt_loss = opt_loss
        self.loss_grad = jax.jit(jax.grad(self.opt_loss))

    def _is_params_valid(self, params):
        """
        Checks if parameters contain NaN or infinite values.
        
        Args:
            params: Parameter tree to validate
            
        Returns:
            bool: True if all parameters are finite, False otherwise
        """
        def is_finite_leaf(x):
            return jnp.all(jnp.isfinite(x))
        
        # Check all leaves in the parameter tree
        finite_flags = jax.tree_util.tree_map(is_finite_leaf, params)
        
        # All leaves must be finite
        return jax.tree_util.tree_reduce(jnp.logical_and, finite_flags, True)

    def _create_perturbed_params(self, base_params, magnitude, rng_key):
        """
        Creates perturbed parameters from valid base parameters.
        
        Args:
            base_params: Valid parameter tree to perturb
            magnitude: Magnitude of perturbation
            rng_key: JAX random key
            
        Returns:
            Perturbed parameter tree
        """
        def add_noise_to_leaf(leaf, key):
            noise = jax.random.normal(key, leaf.shape) * magnitude
            return leaf + noise
        
        # Generate keys for each leaf
        treedef = jax.tree_util.tree_structure(base_params)
        num_leaves = treedef.num_leaves
        keys = jax.random.split(rng_key, num_leaves)
        
        # Apply noise to each leaf
        perturbed_params = jax.tree_util.tree_map(
            add_noise_to_leaf, base_params, 
            jax.tree_util.tree_unflatten(treedef, keys)
        )
        
        return perturbed_params

    def _log_retry_attempt(self, iteration, retry_count, error_type):
        """
        Logs retry attempts based on verbosity settings.
        
        Args:
            iteration: Current optimization iteration
            retry_count: Number of retry attempt
            error_type: Type of numerical error detected
        """
        verbosity = settings.options.get('OPTIMIZATION_VERBOSITY', 0)
        
        if verbosity >= 1:
            message = (f"Numerical instability detected at iteration {iteration}. "
                      f"Retry attempt {retry_count}: {error_type}")
            warnings.warn(message, RuntimeWarning, stacklevel=4)
            
        if verbosity >= 2:
            logger = logging.getLogger(__name__)
            logger.debug(f"Optimization retry: iter={iteration}, "
                        f"attempt={retry_count}, error={error_type}")

    def optimize(self, optimizer=None, max_iter=1000):

        """
        Optimizes the curve's auxiliary parameters using Optax.
        The parameters are updated based on the last iteration
        of the optimizer. The params_history attribute is also set.

        This method includes automatic handling of numerical instabilities
        that may occur during optimization. When NaN or infinite values
        are detected in the parameters, the optimizer will attempt to
        recover by perturbing the last known valid parameters and retrying.

        Args:
            optimizer (optax optimizer): The optimizer instance from Optax.
            max_iter (int): The maximum number of iterations.

        Raises: 
            RuntimeError: If the parameters are not set.

        Notes:
        (1) If an optimizer is not supplied, a simple gradient descent
            is implemented with learning_rate = 0.01.

        (2) If string-based indexing is used, optimization information
            is obtained at the optimization step which corresponds to the
            integer value of the string or the respective slice.
            
        (3) Numerical stability is handled automatically using settings:
            - MAX_OPTIMIZATION_RETRIES: Maximum retry attempts (default: 3)
            - PERTURBATION_MAGNITUDE: Size of parameter perturbation (default: 1e-6)
            - OPTIMIZATION_VERBOSITY: Logging level (default: 0)
            - NUMERICAL_CHECK_FREQUENCY: Check frequency (default: 1)
        """

        # Load numerical stability settings
        max_retries = settings.options.get('MAX_OPTIMIZATION_RETRIES', 3)
        perturbation_mag = settings.options.get('PERTURBATION_MAGNITUDE', 1e-6)
        verbosity = settings.options.get('OPTIMIZATION_VERBOSITY', 0)
        check_freq = settings.options.get('NUMERICAL_CHECK_FREQUENCY', 1)
        
        if optimizer is None:
            optimizer = optax.scale(-0.01)

        @jax.jit
        def step(params, opt_state):

            grads = self.loss_grad(params)

            updates, opt_state = optimizer.update(grads, opt_state, params)

            params = optax.apply_updates(params, updates)

            return params, opt_state

        params = self.params

        if params is None:
            raise RuntimeError(
                'The parameters are not set.'
                ' Use the .initialize_parameters() for initialization.')

        # Initialize numerical stability tracking
        last_valid_params = params
        last_valid_opt_state = optimizer.init(params)
        rng_key = jax.random.PRNGKey(0)
        
        # Check initial parameters
        if not self._is_params_valid(params):
            raise RuntimeError(
                'Initial parameters contain NaN or infinite values. '
                'Please check parameter initialization.')

        opt_state = optimizer.init(params)
        params_history = []

        for iteration in progbar_range(max_iter, title='Optimizing parameters'):

            params_history.append(params)

            # Periodic numerical stability check
            if iteration % check_freq == 0 and not self._is_params_valid(params):
                
                recovery_successful = False
                
                for retry in range(max_retries):
                    self._log_retry_attempt(iteration, retry + 1, "NaN/Inf detected")
                    
                    # Perturb last valid parameters
                    rng_key, subkey = jax.random.split(rng_key)
                    params = self._create_perturbed_params(
                        last_valid_params, perturbation_mag, subkey)
                    opt_state = last_valid_opt_state
                    
                    # Check if perturbation resolved the issue
                    if self._is_params_valid(params):
                        recovery_successful = True
                        if verbosity >= 1:
                            warnings.warn(
                                f"Successfully recovered from numerical instability "
                                f"at iteration {iteration} after {retry + 1} attempts.",
                                RuntimeWarning, stacklevel=2)
                        break
                
                if not recovery_successful:
                    # All retry attempts failed
                    error_message = (
                        f"Critical numerical instability at iteration {iteration}. "
                        f"Failed to recover after {max_retries} attempts. "
                        f"Optimization results may be unreliable. "
                        f"Consider: (1) checking initial parameters, "
                        f"(2) reducing learning rate, (3) using different optimizer, "
                        f"(4) increasing PERTURBATION_MAGNITUDE in settings."
                    )
                    
                    if verbosity >= 0:  # Always warn for critical failures
                        warnings.warn(error_message, RuntimeWarning, stacklevel=2)
                    
                    # Continue with last valid parameters as fallback
                    params = last_valid_params
                    opt_state = last_valid_opt_state

            # Perform optimization step
            try:
                new_params, new_opt_state = step(params, opt_state)
                
                # Validate the step result
                if self._is_params_valid(new_params):
                    # Successful step - update tracking variables
                    params = new_params
                    opt_state = new_opt_state
                    last_valid_params = params
                    last_valid_opt_state = opt_state
                else:
                    # Step produced invalid parameters - use recovery
                    if verbosity >= 1:
                        warnings.warn(
                            f"Optimization step at iteration {iteration} "
                            f"produced invalid parameters. Using last valid state.",
                            RuntimeWarning, stacklevel=2)
                    # Keep current valid parameters
                    
            except Exception as e:
                # Optimization step failed completely
                if verbosity >= 1:
                    warnings.warn(
                        f"Optimization step failed at iteration {iteration}: {e}. "
                        f"Using last valid parameters.",
                        RuntimeWarning, stacklevel=2)
                # Keep current valid parameters
                params = last_valid_params
                opt_state = last_valid_opt_state

        params_history.append(params)
        self.params_history = params_history

        self.set_params(params_history[-1])

    def get_params_history(self):
        return self.params_history

    def __getitem__(self, index):

        if isinstance(index, str):

            if ':' not in index:
                index = int(index)
                param_value = self.params_history[index]

                return {
                    'param_value': param_value,
                    'loss_value': self.opt_loss(param_value),
                    'loss_grad_value': self.loss_grad(param_value)}

            index = slice(*map(int, index.split(':')))
            param_value = jnp.array(self.params_history[index])
            return {
                'param_value': param_value,
                'loss_value': jax.vmap(self.opt_loss)(param_value),
                'loss_grad_value': jax.vmap(self.loss_grad)(param_value)}

        return super().__getitem__(index)

    def update_params_from_opt_history(self, opt_step=-1):
        self.set_params(self.params_history[opt_step])


class BarqCurve(OptimizableSpaceCurve):

    """
    The BarqCurve implements the BARQ method that provides the optimal control
    points for the Bezier curve based on a loss function.
    See the paper for the description of the Point Configuration (PC) which
    defines the PGF and the PRS. Their implementation can be found in
    barqtools.py

    Attributes:
        n_free_points: The number of free points used in BARQ.
        barq_fun: The map created with free_points, PGF and PRS to the Bezier
        control points.

    Methods:
        initialize_parameters: Initializes the various parameters involved in
        BARQ.
        get_bezier_control_points: Returns the Bezier curve control points used
        in BARQ.
        save_bezier_control_points: Stores the control points in a csv file.
        See the method implementation for details.

    Notes:

    The pgf_mod function is defined as:

    def pgf_mod(pgf_params, input_points):
        new_pgf_params = pgf_params.copy()
        ...
        return new_pgf_params

    The pgf_params provide flexibility on the gate-fixing stage of the BARQ
    method. The input points are the free points of the BARQ method.

    The prs_fun function is defined as:

    def prs_fun(prs_params, input_points):

        return internal_points

    At this step, the input points is an (6 + (n_free_points-2)) x 3 array.
    The first 6 points correspond to the control points which participate
    in the gate-fixing process and the rest are the free_points
    (the first two are excluded since they were already included in the first
    6 input points).

    A prs_fun that simply allows the free_points to pass acts as:
    prs_params, input_points -> input_points[6:, :].
    """

    def __init__(self, *, adj_target, n_free_points,
                 pgf_mod=None, prs_fun=None):

        """
        Initializes the BARQ method.

        Args:
            adj_target (array): The adjoint representation of the target
            operation.
            n_free_points (int): The number of free points
            for the BARQ method.
            pgf_mod (function): A function that modifies the PGF parameters.
            prs_fun (function): A function that enforces a particular structure
            on the internal points of the curve.
        """

        self.n_free_points = n_free_points

        barq_fun = barqtools.make_barq_fun(adj_target, pgf_mod, prs_fun)
        self.barq_fun = barq_fun

        bezier_deriv_array_fun = beziertools.make_bezier_deriv_array_fun()

        def barq_derivs_fun(x, params):

            W = barq_fun(params)

            return bezier_deriv_array_fun(x, W)

        def barq_curve(x, params):

            W = barq_fun(params)

            return beziertools.bezier_curve_vec(x, W)

        super().__init__(curve=barq_curve,
                         order=0,
                         interval=[0, 1],
                         deriv_array_fun=barq_derivs_fun)

    def initialize_parameters(self, *, init_free_points=None,
                              init_pgf_params=None,
                              init_prs_params=None, seed=None):

        """
        Initializes the parameters for the BARQ method.
        The parameters are passed to the curve as a dictionary with entries
        containing the free points, the pgf parameters and the prs parameters.

        If no initial free points are provided, a total of n_free_points random
        points are drawn and normalized to unit magnitude.

        Raises:
            ValueError: If the number of free points provided upon
            instantiation do not agree with the dimensions of the initial
            points.
        """

        params = {}

        if seed is None:
            seed = 0

        rng = numpy.random.default_rng(seed)

        if init_free_points is None:

            init_free_points = rng.standard_normal((self.n_free_points, 3))
            init_free_points = \
                init_free_points/numpy.linalg.norm(init_free_points, axis=0)

            init_free_points = jnp.array(init_free_points)

        else:
            if init_free_points.shape[0] != self.n_free_points:
                raise ValueError('Inconsistent number of free points with'
                                 ' provided initial free points.')

        if init_pgf_params is None:

            init_pgf_params = barqtools.get_default_pgf_params_dict()

        if init_prs_params is None:
            init_prs_params = {}

        params['free_points'] = init_free_points
        params['pgf_params'] = init_pgf_params
        params['prs_params'] = init_prs_params

        return super().initialize_parameters(params)

    def evaluate_control_dict(self, n_points=None):

        """
        Evaluates the control dictionary using the TTC choice.
        """

        # If the pgf_mod fixes some parameters, they will not be automatically
        # updated upon execution. The corner case is when the barq_angle
        # is fixed, and the TTC used. That case must be handled with an
        # additional update or by masking the respective gradient update.

        super().evaluate_control_dict('TTC', n_points)

    def get_bezier_control_points(self):

        """
        Returns the control points used in BARQ.
        """

        return self.barq_fun(self.params)

    def save_bezier_control_points(self, filename):

        """
        Saves the control points of the associated Bezier curve.
        The first row contains the value of the binormal angle and the rest
        contain the control points used in BARQ.
        """

        points = self.get_bezier_control_points()
        barq_angle = self.params['pgf_params']['barq_angle']

        # We add the first line to store the binormal angle for the TTC.

        points = jnp.vstack([
            [barq_angle, -1., -1.],
            points
        ])

        df = pd.DataFrame(points)
        df.to_csv(filename, index=False, float_format='%.12f')
