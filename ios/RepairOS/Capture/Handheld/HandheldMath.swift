import Foundation
import simd

// Rigid-body helpers for the hand-held ("Turn it") scan. Pure Swift + simd (no ARKit), so the tracker also builds on
// the Mac for the offline replay harness (tools/handheld/).
//
// Frames: ARKit world (metres, Y up); the OBJECT frame is the ARKit world frame at the moment the object was locked
// (it rested on the table then), so the locked box, the support plane and every earlier point stay valid in it.
// `objectToWorld` (W) moves object-frame points to where the object is now; the camera relative to the object is
// W⁻¹ · cameraToWorld.

enum Rigid {
    static let identity = matrix_identity_float4x4

    static func make(_ r: simd_float3x3, _ t: SIMD3<Float>) -> simd_float4x4 {
        simd_float4x4(columns: (SIMD4(r.columns.0, 0), SIMD4(r.columns.1, 0), SIMD4(r.columns.2, 0), SIMD4(t, 1)))
    }

    @inline(__always) static func rotation(_ m: simd_float4x4) -> simd_float3x3 {
        simd_float3x3(columns: (m.columns.0.xyz3, m.columns.1.xyz3, m.columns.2.xyz3))
    }

    @inline(__always) static func translation(_ m: simd_float4x4) -> SIMD3<Float> { m.columns.3.xyz3 }

    /// Inverse of a rigid transform (rotation transposed).
    static func inverse(_ m: simd_float4x4) -> simd_float4x4 {
        let rt = rotation(m).transpose
        return make(rt, -(rt * translation(m)))
    }

    @inline(__always) static func apply(_ m: simd_float4x4, _ p: SIMD3<Float>) -> SIMD3<Float> {
        let r = m * SIMD4(p, 1)
        return SIMD3(r.x, r.y, r.z)
    }

    /// Rotation by the rotation vector `w` (axis × angle, radians), Rodrigues.
    static func rotation(vector w: SIMD3<Float>) -> simd_float3x3 {
        let a = simd_length(w)
        guard a > 1e-9 else { return matrix_identity_float3x3 }
        return simd_float3x3(simd_quatf(angle: a, axis: w / a))
    }

    /// The rotation vector of a rotation matrix (axis × angle).
    static func rotationVector(_ r: simd_float3x3) -> SIMD3<Float> {
        let q = simd_quatf(orthonormalized(r))
        let a = q.angle
        guard a > 1e-7 else { return .zero }
        let axis = q.axis
        return (a > .pi ? a - 2 * .pi : a) * axis
    }

    /// The rotation angle between two rigid transforms (radians).
    static func angle(_ a: simd_float4x4, _ b: simd_float4x4) -> Float {
        let r = rotation(a).transpose * rotation(b)
        let c = (r[0][0] + r[1][1] + r[2][2] - 1) / 2
        return acos(max(-1, min(1, c)))
    }

    /// The distance between where two poses put the points `probe` (the larger one): a size-aware pose distance.
    static func distance(_ a: simd_float4x4, _ b: simd_float4x4, probe: [SIMD3<Float>]) -> Float {
        probe.map { simd_distance(apply(a, $0), apply(b, $0)) }.max() ?? 0
    }

    /// Nearest rotation (polar decomposition by a few Newton steps), so accumulated updates stay rigid.
    static func orthonormalized(_ r: simd_float3x3) -> simd_float3x3 {
        var x = r
        for _ in 0..<3 {
            let xi = x.inverse.transpose
            x = (x + xi) * 0.5
        }
        return x
    }

    static func orthonormalized(_ m: simd_float4x4) -> simd_float4x4 { make(orthonormalized(rotation(m)), translation(m)) }

    /// Constant velocity: the motion from pose `a` (time ta) to pose `b` (time tb), continued to time t:
    /// Δ = b · a⁻¹ split into a rotation about `pivot` (the object's centre in world at b, what a turning hand
    /// rotates about) and the pivot's displacement, both scaled by s = (t − tb) / (tb − ta), capped at `maxStep`.
    static func extrapolate(_ a: simd_float4x4, at ta: Double, _ b: simd_float4x4, at tb: Double, to t: Double,
                            pivot: SIMD3<Float>, maxStep: Double = 1.5) -> simd_float4x4 {
        guard tb > ta + 1e-4 else { return b }
        let s = Float(min(maxStep, max(0, (t - tb) / (tb - ta))))
        guard s > 0 else { return b }
        let d = b * inverse(a)
        // Where Δ took the pivot one step back: p₀ = Δ⁻¹ p; the pivot's displacement over a step is p − p₀.
        let p0 = apply(inverse(d), pivot)
        let r = rotation(vector: rotationVector(rotation(d)) * s)
        let move = (pivot - p0) * s
        let step = make(r, pivot - r * pivot + move)
        return orthonormalized(step * b)
    }

    /// The motion between two poses in a form the stillness gate reads: linear (m) at `probe` and angle (rad).
    static func motion(_ a: simd_float4x4, _ b: simd_float4x4, probe: SIMD3<Float>) -> (linear: Float, angle: Float) {
        (simd_distance(apply(a, probe), apply(b, probe)), angle(a, b))
    }
}

extension SIMD4 where Scalar == Float {
    @inline(__always) var xyz3: SIMD3<Float> { SIMD3(x, y, z) }
}

/// Small dense linear algebra in Double (the normal equations stay well conditioned in Double).
enum DenseSolve {
    /// Solves the n × n symmetric positive definite system `a x = b` by Cholesky (row-major `a`), nil if singular.
    static func cholesky(_ a: [Double], _ b: [Double], n: Int) -> [Double]? {
        var l = [Double](repeating: 0, count: n * n)
        for i in 0..<n {
            for j in 0...i {
                var s = a[i * n + j]
                for k in 0..<j { s -= l[i * n + k] * l[j * n + k] }
                if i == j {
                    guard s > 1e-18 else { return nil }
                    l[i * n + i] = s.squareRoot()
                } else {
                    l[i * n + j] = s / l[j * n + j]
                }
            }
        }
        var y = [Double](repeating: 0, count: n)
        for i in 0..<n {
            var s = b[i]
            for k in 0..<i { s -= l[i * n + k] * y[k] }
            y[i] = s / l[i * n + i]
        }
        var x = [Double](repeating: 0, count: n)
        for i in stride(from: n - 1, through: 0, by: -1) {
            var s = y[i]
            for k in (i + 1)..<n { s -= l[k * n + i] * x[k] }
            x[i] = s / l[i * n + i]
        }
        return x
    }

    /// Eigen decomposition of a symmetric n × n matrix (row-major) by cyclic Jacobi rotations: (values, vectors as
    /// columns, row-major).
    static func symmetricEigen(_ m: [Double], n: Int, sweeps: Int = 16) -> (values: [Double], vectors: [Double]) {
        var a = m
        var v = [Double](repeating: 0, count: n * n)
        for i in 0..<n { v[i * n + i] = 1 }
        for _ in 0..<sweeps {
            var off = 0.0
            for p in 0..<n { for q in (p + 1)..<n { off += a[p * n + q] * a[p * n + q] } }
            if off < 1e-22 { break }
            for p in 0..<n {
                for q in (p + 1)..<n where abs(a[p * n + q]) > 1e-300 {
                    let theta = (a[q * n + q] - a[p * n + p]) / (2 * a[p * n + q])
                    let t = (theta >= 0 ? 1.0 : -1.0) / (abs(theta) + (theta * theta + 1).squareRoot())
                    let c = 1 / (t * t + 1).squareRoot(), s = t * c
                    for k in 0..<n {
                        let akp = a[k * n + p], akq = a[k * n + q]
                        a[k * n + p] = c * akp - s * akq
                        a[k * n + q] = s * akp + c * akq
                    }
                    for k in 0..<n {
                        let apk = a[p * n + k], aqk = a[q * n + k]
                        a[p * n + k] = c * apk - s * aqk
                        a[q * n + k] = s * apk + c * aqk
                    }
                    for k in 0..<n {
                        let vkp = v[k * n + p], vkq = v[k * n + q]
                        v[k * n + p] = c * vkp - s * vkq
                        v[k * n + q] = s * vkp + c * vkq
                    }
                }
            }
        }
        return ((0..<n).map { a[$0 * n + $0] }, v)
    }

    /// The rigid transform (R, t) minimising Σ wᵢ |R·srcᵢ + t − dstᵢ|² (Horn 1987, closed form by the unit
    /// quaternion: the top eigenvector of the 4 × 4 matrix built from the cross-covariance). nil for < 3 pairs.
    static func absoluteOrientation(_ src: [SIMD3<Float>], _ dst: [SIMD3<Float>], weights: [Float]? = nil) -> simd_float4x4? {
        let n = min(src.count, dst.count)
        guard n >= 3 else { return nil }
        var ws = 0.0
        var cs = SIMD3<Double>.zero, cd = SIMD3<Double>.zero
        for i in 0..<n {
            let w = Double(weights?[i] ?? 1)
            ws += w
            cs += w * SIMD3<Double>(src[i])
            cd += w * SIMD3<Double>(dst[i])
        }
        guard ws > 1e-12 else { return nil }
        cs /= ws
        cd /= ws
        var s = [Double](repeating: 0, count: 9)   // S[a][b] = Σ w (src_a)(dst_b), centred
        for i in 0..<n {
            let w = Double(weights?[i] ?? 1)
            let a = SIMD3<Double>(src[i]) - cs, b = SIMD3<Double>(dst[i]) - cd
            for r in 0..<3 { for c in 0..<3 { s[r * 3 + c] += w * a[r] * b[c] } }
        }
        let sxx = s[0], sxy = s[1], sxz = s[2], syx = s[3], syy = s[4], syz = s[5], szx = s[6], szy = s[7], szz = s[8]
        let nm: [Double] = [
            sxx + syy + szz, syz - szy, szx - sxz, sxy - syx,
            syz - szy, sxx - syy - szz, sxy + syx, szx + sxz,
            szx - sxz, sxy + syx, -sxx + syy - szz, syz + szy,
            sxy - syx, szx + sxz, syz + szy, -sxx - syy + szz,
        ]
        let (values, vectors) = symmetricEigen(nm, n: 4)
        guard let top = (0..<4).max(by: { values[$0] < values[$1] }) else { return nil }
        let qw = vectors[0 * 4 + top], qx = vectors[1 * 4 + top], qy = vectors[2 * 4 + top], qz = vectors[3 * 4 + top]
        let q = simd_quatf(ix: Float(qx), iy: Float(qy), iz: Float(qz), r: Float(qw)).normalized
        let r = simd_float3x3(q)
        let t = SIMD3<Float>(cd) - r * SIMD3<Float>(cs)
        return Rigid.make(r, t)
    }
}

/// A small deterministic generator (tests and the BRIEF pattern must be reproducible).
struct SplitMix64: RandomNumberGenerator {
    var state: UInt64
    init(seed: UInt64) { state = seed }
    mutating func next() -> UInt64 {
        state &+= 0x9E37_79B9_7F4A_7C15
        var z = state
        z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
        z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
        return z ^ (z >> 31)
    }

    /// A standard normal sample (Box–Muller).
    mutating func gaussian() -> Float {
        let u1 = max(1e-12, Double.random(in: 0..<1, using: &self)), u2 = Double.random(in: 0..<1, using: &self)
        return Float((-2 * Foundation.log(u1)).squareRoot() * cos(2 * .pi * u2))
    }
}

/// The locked object box in the object frame (the tracker's own copy of ObjectBox, free of app types): an upright
/// box turned by `yaw` about +Y.
struct TrackBox: Sendable, Equatable {
    var center: SIMD3<Float>
    var half: SIMD3<Float>
    var yaw: Float

    var planeY: Float { center.y - half.y }

    @inline(__always) func local(_ p: SIMD3<Float>) -> SIMD3<Float> {
        let d = p - center
        let c = cos(-yaw), s = sin(-yaw)
        return SIMD3(c * d.x + s * d.z, d.y, -s * d.x + c * d.z)
    }

    @inline(__always) func contains(_ p: SIMD3<Float>, margin: Float = 0) -> Bool {
        let l = local(p)
        return abs(l.x) <= half.x + margin && abs(l.y) <= half.y + margin && abs(l.z) <= half.z + margin
    }

    /// Eight corners, for pose-distance probes.
    var corners: [SIMD3<Float>] {
        let r = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
        return [-1, 1].flatMap { x in [-1, 1].flatMap { y in [-1, 1].map { z in
            center + r.act(SIMD3(Float(x) * half.x, Float(y) * half.y, Float(z) * half.z))
        } } }
    }
}
