import time
import numpy as np
from scipy.linalg import cho_factor, cho_solve

def soft_threshold(v, g):
    """
    Soft-thresholding operator: S_g(v) = sign(v) * max(|v| - g, 0)
    """
    return np.sign(v) * np.maximum(np.abs(v) - g, 0.0)

def admm_lasso(A, y, lmbda, rho=1.0, alpha=1.5, max_iter=1000, 
               abstol=1e-4, reltol=1e-2, adaptive_rho=True, 
               rho_update_freq=10, mu=10.0, tau=2.0, verbose=False):
    """
    Solves the Lasso problem:
        min  (1/2) * ||A*x - y||_2^2 + lmbda * ||x||_1
    using the Alternating Direction Method of Multipliers (ADMM).

    Parameters:
    -----------
    A : ndarray of shape (m, n)
        Feature matrix.
    y : ndarray of shape (m,)
        Response vector.
    lmbda : float
        Regularization parameter (lambda > 0).
    rho : float
        Initial augmented Lagrangian penalty parameter.
    alpha : float
        Over-relaxation parameter (typically between 1.0 and 1.8).
    max_iter : int
        Maximum number of iterations.
    abstol : float
        Absolute tolerance for the stopping criterion.
    reltol : float
        Relative tolerance for the stopping criterion.
    adaptive_rho : bool
        If True, dynamically adjust rho using residual balancing.
    rho_update_freq : int
        Frequency (in iterations) to check and update rho.
    mu : float
        Residual balancing threshold ratio.
    tau : float
        Scale factor by which to adjust rho.
    verbose : bool
        Whether to print convergence progress.

    Returns:
    --------
    x : ndarray of shape (n,)
        Estimated coefficient vector.
    history : dict
        Dictionary containing lists of primal/dual residuals and tolerances per iteration.
    """
    m, n = A.shape
    
    # Precompute A^T * y
    At_y = A.T @ y
    
    # Precompute Gram matrix depending on shape
    is_tall = (m >= n)
    if is_tall:
        AtA = A.T @ A
    else:
        AAt = A @ A.T

    # Helper function to compute/update Cholesky factor
    def get_cholesky_factor(r):
        if is_tall:
            # Factorize (A^T * A + r * I)
            return cho_factor(AtA + r * np.eye(n))
        else:
            # Factorize (I + (1/r) * A * A^T)
            return cho_factor(np.eye(m) + (1.0 / r) * AAt)

    # Initialize variables
    x = np.zeros(n)
    z = np.zeros(n)
    u = np.zeros(n)
    
    # Initial Cholesky factorization
    chol_factorization = get_cholesky_factor(rho)
    
    history = {
        "objval": [],
        "r_norm": [],
        "s_norm": [],
        "eps_pri": [],
        "eps_dual": []
    }
    
    if verbose:
        print(f"{'Iter':<5} | {'Obj Val':<12} | {'Pri Res':<10} | {'Pri Tol':<10} | {'Dual Res':<10} | {'Dual Tol':<10} | {'rho':<6}")
        print("-" * 80)
        
    for k in range(1, max_iter + 1):
        # 1. x-update
        q = At_y + rho * (z - u)
        if is_tall:
            x = cho_solve(chol_factorization, q)
        else:
            # Using Sherman-Morrison-Woodbury identity:
            # (A^T * A + rho * I)^-1 * q = (q - A^T * (I + (1/rho)*A*A^T)^-1 * A * q / rho) / rho
            v = cho_solve(chol_factorization, A @ q)
            x = (q - (A.T @ v) / rho) / rho
            
        # 2. Over-relaxation (relaxation step)
        x_hat = alpha * x + (1.0 - alpha) * z
        
        # 3. z-update (Soft-thresholding / Shrinkage)
        z_old = z.copy()
        z = soft_threshold(x_hat + u, lmbda / rho)
        
        # 4. u-update (Dual update)
        u = u + x_hat - z
        
        # 5. Convergence check (Boyd et al. Section 3.3.1)
        r_norm = np.linalg.norm(x - z)
        s_norm = np.linalg.norm(-rho * (z - z_old))
        
        eps_pri = np.sqrt(n) * abstol + reltol * max(np.linalg.norm(x), np.linalg.norm(z))
        eps_dual = np.sqrt(n) * abstol + reltol * np.linalg.norm(rho * u)
        
        objval = 0.5 * np.sum((A @ x - y) ** 2) + lmbda * np.linalg.norm(x, 1)
        
        history["objval"].append(objval)
        history["r_norm"].append(r_norm)
        history["s_norm"].append(s_norm)
        history["eps_pri"].append(eps_pri)
        history["eps_dual"].append(eps_dual)
        
        if verbose and (k == 1 or k % 50 == 0 or k == max_iter):
            print(f"{k:<5} | {objval:<12.4f} | {r_norm:<10.4f} | {eps_pri:<10.4f} | {s_norm:<10.4f} | {eps_dual:<10.4f} | {rho:<6.2f}")
            
        if r_norm <= eps_pri and s_norm <= eps_dual:
            if verbose:
                print(f"Converged at iteration {k}")
            break
            
        # 6. Adaptive Rho / Residual Balancing (Boyd et al. Section 3.4.1)
        if adaptive_rho and (k % rho_update_freq == 0):
            if r_norm > mu * s_norm:
                rho *= tau
                u /= tau
                chol_factorization = get_cholesky_factor(rho)
            elif s_norm > mu * r_norm:
                rho /= tau
                u *= tau
                chol_factorization = get_cholesky_factor(rho)
                
    return x, history

if __name__ == "__main__":
    # Test harness
    print("Generating synthetic data...")
    np.random.seed(42)
    m = 100
    n = 500
    
    # Create sparse coefficient vector
    true_x = np.zeros(n)
    active_indices = np.random.choice(n, 15, replace=False)
    true_x[active_indices] = np.random.randn(15)
    
    # Feature matrix and response
    A = np.random.randn(m, n)
    # Normalize columns of A
    A = A / np.linalg.norm(A, axis=0)
    y = A @ true_x + 0.1 * np.random.randn(m)
    
    lmbda = 0.1
    
    print(f"Running ADMM Lasso (m={m}, n={n}, lambda={lmbda})...")
    start_time = time.time()
    x_est, hist = admm_lasso(A, y, lmbda, rho=1.0, alpha=1.5, max_iter=500, verbose=True)
    end_time = time.time()
    print(f"ADMM completed in {end_time - start_time:.4f} seconds.")
    
    # Compare with Scikit-Learn if installed
    try:
        from sklearn.linear_model import Lasso
        clf = Lasso(alpha=lmbda / m, fit_intercept=False, tol=1e-6, max_iter=10000)
        clf.fit(A * np.sqrt(m), y * np.sqrt(m)) # scale to match 1/2 ||Ax - y||^2
        
        # Or fit using standard objective by matching scale:
        # sklearn objective: 1 / (2 * n_samples) * ||y - Xw||^2_2 + alpha * ||w||_1
        # To match our objective: 1/2 * ||Ax - y||^2 + lmbda * ||x||_1,
        # we can set sklearn's alpha = lmbda / m, and fit X = A, y = y directly
        clf_direct = Lasso(alpha=lmbda / m, fit_intercept=False, max_iter=10000, tol=1e-6)
        clf_direct.fit(A, y)
        
        print("\nComparison with scikit-learn Lasso:")
        print(f"ADMM Final Objective: {hist['objval'][-1]:.6f}")
        sk_obj = 0.5 * np.sum((A @ clf_direct.coef_ - y) ** 2) + lmbda * np.linalg.norm(clf_direct.coef_, 1)
        print(f"Sklearn Final Objective: {sk_obj:.6f}")
        
        mse = np.mean((x_est - clf_direct.coef_) ** 2)
        print(f"Mean Squared Difference between coefficients: {mse:.2e}")
    except ImportError:
        print("\nScikit-learn not available. Skipping comparison.")
        print(f"ADMM Final Objective: {hist['objval'][-1]:.6f}")
