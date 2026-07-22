//! Minimal dense symmetric linear algebra for the CMA-ES core.
//!
//! Hand-rolled by design (`DEC-RUST-STAGE3-REFINE-001` crate decision): the
//! Stage-3 search is 2-dimensional, so a cyclic-Jacobi eigendecomposition
//! over row-major `Vec<f64>` matrices is exact enough, fully deterministic
//! (fixed sweep order, no threading) and keeps the crate dependency-free —
//! no nalgebra/BLAS, no platform-varying kernels.

/// Row-major dense n×n matrix helper: `a[i * n + j]`.
#[inline]
pub fn idx(n: usize, i: usize, j: usize) -> usize {
    i * n + j
}

/// Eigendecomposition of a symmetric matrix via cyclic Jacobi rotations.
///
/// Returns `(eigenvalues, eigenvectors)` where `eigenvectors` is row-major
/// n×n with EIGENVECTOR k stored as COLUMN k (`v[i*n + k]`), matching
/// `A = V · diag(λ) · Vᵀ`. Eigenpairs are sorted ASCENDING by eigenvalue
/// (deterministic tie order: stable sort by value, index breaks ties).
///
/// Convergence: sweeps until the off-diagonal Frobenius mass is below
/// `1e-30 ×` the total Frobenius mass (or 100 sweeps — a hard determinism
/// bound; for the CMA-ES 2×2/low-n covariance this converges in a handful).
pub fn sym_eigen(a: &[f64], n: usize) -> (Vec<f64>, Vec<f64>) {
    assert_eq!(a.len(), n * n, "matrix shape mismatch");
    let mut m = a.to_vec();
    // V starts as identity.
    let mut v = vec![0.0; n * n];
    for i in 0..n {
        v[idx(n, i, i)] = 1.0;
    }
    let frob: f64 = m.iter().map(|x| x * x).sum();
    let tol = 1e-30 * frob.max(f64::MIN_POSITIVE);
    for _sweep in 0..100 {
        let off: f64 = (0..n)
            .flat_map(|i| (0..n).filter(move |j| *j != i).map(move |j| (i, j)))
            .map(|(i, j)| m[idx(n, i, j)] * m[idx(n, i, j)])
            .sum();
        if off <= tol {
            break;
        }
        for p in 0..n - 1 {
            for q in p + 1..n {
                let apq = m[idx(n, p, q)];
                if apq == 0.0 {
                    continue;
                }
                let app = m[idx(n, p, p)];
                let aqq = m[idx(n, q, q)];
                // Classic Jacobi rotation angle (Golub & Van Loan 8.4).
                let tau = (aqq - app) / (2.0 * apq);
                let t = if tau >= 0.0 {
                    1.0 / (tau + (1.0 + tau * tau).sqrt())
                } else {
                    1.0 / (tau - (1.0 + tau * tau).sqrt())
                };
                let c = 1.0 / (1.0 + t * t).sqrt();
                let s = t * c;
                // Update rows/columns p and q of the symmetric matrix.
                for k in 0..n {
                    let akp = m[idx(n, k, p)];
                    let akq = m[idx(n, k, q)];
                    m[idx(n, k, p)] = c * akp - s * akq;
                    m[idx(n, k, q)] = s * akp + c * akq;
                }
                for k in 0..n {
                    let apk = m[idx(n, p, k)];
                    let aqk = m[idx(n, q, k)];
                    m[idx(n, p, k)] = c * apk - s * aqk;
                    m[idx(n, q, k)] = s * apk + c * aqk;
                }
                // Symmetrize the rotated pair exactly.
                m[idx(n, p, q)] = 0.0;
                m[idx(n, q, p)] = 0.0;
                // Accumulate the rotation into V (columns p, q).
                for k in 0..n {
                    let vkp = v[idx(n, k, p)];
                    let vkq = v[idx(n, k, q)];
                    v[idx(n, k, p)] = c * vkp - s * vkq;
                    v[idx(n, k, q)] = s * vkp + c * vkq;
                }
            }
        }
    }
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&i, &j| {
        m[idx(n, i, i)]
            .partial_cmp(&m[idx(n, j, j)])
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(i.cmp(&j))
    });
    let eigvals: Vec<f64> = order.iter().map(|&k| m[idx(n, k, k)]).collect();
    let mut eigvecs = vec![0.0; n * n];
    for (new_col, &old_col) in order.iter().enumerate() {
        for row in 0..n {
            eigvecs[idx(n, row, new_col)] = v[idx(n, row, old_col)];
        }
    }
    (eigvals, eigvecs)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn assert_close(a: f64, b: f64, tol: f64) {
        assert!((a - b).abs() <= tol, "{a} vs {b} (tol {tol})");
    }

    #[test]
    fn identity_eigen() {
        let (vals, vecs) = sym_eigen(&[1.0, 0.0, 0.0, 1.0], 2);
        assert_eq!(vals, vec![1.0, 1.0]);
        // Columns orthonormal.
        let dot = vecs[0] * vecs[1] + vecs[2] * vecs[3];
        assert_close(dot, 0.0, 1e-12);
    }

    #[test]
    fn known_2x2() {
        // [[2, 1], [1, 2]] — eigenvalues 1 and 3.
        let (vals, _) = sym_eigen(&[2.0, 1.0, 1.0, 2.0], 2);
        assert_close(vals[0], 1.0, 1e-12);
        assert_close(vals[1], 3.0, 1e-12);
    }

    #[test]
    fn reconstructs_matrix() {
        let a = [4.0, 1.5, 1.5, 3.0];
        let n = 2;
        let (vals, v) = sym_eigen(&a, n);
        // A ?= V diag(vals) V^T
        for i in 0..n {
            for j in 0..n {
                let mut acc = 0.0;
                for k in 0..n {
                    acc += v[idx(n, i, k)] * vals[k] * v[idx(n, j, k)];
                }
                assert_close(acc, a[idx(n, i, j)], 1e-10);
            }
        }
    }

    #[test]
    fn three_by_three() {
        // diag(1,2,3) rotated is still spectrum {1,2,3}; use a plain
        // symmetric 3×3 with known trace/determinant checks.
        let a = [2.0, -1.0, 0.0, -1.0, 2.0, -1.0, 0.0, -1.0, 2.0];
        let (vals, _) = sym_eigen(&a, 3);
        // Known spectrum: 2 - sqrt(2), 2, 2 + sqrt(2).
        assert_close(vals[0], 2.0 - std::f64::consts::SQRT_2, 1e-10);
        assert_close(vals[1], 2.0, 1e-10);
        assert_close(vals[2], 2.0 + std::f64::consts::SQRT_2, 1e-10);
    }

    #[test]
    fn deterministic() {
        let a = [4.0, 0.3, 0.3, 5.0];
        let (v1, e1) = sym_eigen(&a, 2);
        let (v2, e2) = sym_eigen(&a, 2);
        assert_eq!(v1, v2);
        assert_eq!(e1, e2);
    }
}
