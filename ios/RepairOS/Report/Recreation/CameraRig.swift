import QuartzCore
import SceneKit
import simd

/// Lea's camera moves as one continuous motion (the owner's bar: as smooth as a product-page scroll). The camera is
/// described as an orbit round a look target (azimuth, elevation, distance) and every part of it follows a critically
/// damped spring toward its goal, so a move travels along an arc round the object (never a straight line through it),
/// eases in and out with no overshoot, and a new goal mid-flight blends from the current velocity (no stop-then-go).
/// It draws at the display's full rate only while moving; touch cancels it at once and leaves the camera where it is.
@MainActor
final class CameraRig {
    struct Orbit: Equatable {
        var target: SIMD3<Float>
        var azimuth: Float
        var elevation: Float
        var distance: Float

        init(target: SIMD3<Float>, azimuth: Float, elevation: Float, distance: Float) {
            self.target = target; self.azimuth = azimuth; self.elevation = elevation; self.distance = distance
        }

        /// The orbit that puts the eye at `eye` looking at `look`.
        init(eye: SIMD3<Float>, look: SIMD3<Float>) {
            let d = eye - look
            let r = max(simd_length(d), 1e-4)
            self.init(target: look, azimuth: atan2(d.x, d.z), elevation: asin(max(-1, min(1, d.y / r))), distance: r)
        }

        var eye: SIMD3<Float> {
            target + distance * SIMD3(cos(elevation) * sin(azimuth), sin(elevation), cos(elevation) * cos(azimuth))
        }
    }

    /// The highest and lowest the camera goes (just short of straight down or up, where the look-at flips).
    nonisolated static let maxElevation: Float = 1.43

    /// The orbit with its elevation within the camera's range. A goal past the clamp in `tick` would never be
    /// reached, so the move would never settle and the display link would keep drawing a still camera.
    nonisolated static func reachable(_ o: Orbit) -> Orbit {
        var o = o
        o.elevation = max(-maxElevation, min(maxElevation, o.elevation))
        return o
    }

    private weak var view: SCNView?
    private let camera: SCNNode
    private var current: Orbit?
    private var velocity = (target: SIMD3<Float>(repeating: 0), azimuth: Float(0), elevation: Float(0), distance: Float(0))
    private var goal: Orbit?
    private var omega: Float = 7
    private var startGap: Float = 1
    private var arrived = false
    private var link: CADisplayLink?
    private var last: CFTimeInterval?
    /// The scale of the scene (metres), so the settle test works for a coin and for a washing machine.
    var scale: Float = 0.1
    /// Called once when the move is about 80% of the way (Motion.Scene.ringStart): the highlight lands then.
    var onArrive: (() -> Void)?
    /// Called when the move has settled (the view can go back to drawing on demand).
    var onSettle: (() -> Void)?

    init(view: SCNView, camera: SCNNode) {
        self.view = view
        self.camera = camera
    }

    var moving: Bool { link != nil }

    /// Moves to `eye` looking at `look`, settling in about `seconds` (Motion.Scene.fly / orbit / zoom). From wherever
    /// the camera is now, with whatever velocity it has (a move in flight blends into the new one).
    func go(eye: SIMD3<Float>, look: SIMD3<Float>, seconds: Double) {
        guard let view else { return }
        camera.removeAllAnimations()
        if current == nil || link == nil {
            let from = camera.presentation.simdWorldPosition
            current = Orbit(eye: from, look: SIMD3<Float>(view.defaultCameraController.target))
            if link == nil { velocity = (SIMD3(repeating: 0), 0, 0, 0) }
        }
        guard let now = current else { return }
        var next = Orbit(eye: eye, look: look)
        next = Self.reachable(next)
        // The short way round.
        while next.azimuth - now.azimuth > .pi { next.azimuth -= 2 * .pi }
        while next.azimuth - now.azimuth < -.pi { next.azimuth += 2 * .pi }
        goal = next
        // Critically damped: about 98% settled at 5.8 / omega.
        omega = 5.8 / Float(max(0.2, seconds))
        startGap = max(gap(now, next), 1e-4)
        arrived = false
        start()
    }

    /// Stops the move where it is (a touch took over).
    func cancel() {
        link?.invalidate()
        link = nil
        last = nil
        goal = nil
        current = nil
    }

    private func start() {
        guard link == nil else { return }
        let l = CADisplayLink(target: self, selector: #selector(tick(_:)))
        l.preferredFrameRateRange = CAFrameRateRange(minimum: 60, maximum: 120, preferred: 120)
        l.add(to: .main, forMode: .common)
        link = l
        view?.isPlaying = true
    }

    /// How far apart two orbits are, in comparable units (radians, and metres over the scene's scale).
    private func gap(_ a: Orbit, _ b: Orbit) -> Float {
        abs(a.azimuth - b.azimuth) + abs(a.elevation - b.elevation)
            + abs(a.distance - b.distance) / scale + simd_length(a.target - b.target) / scale
    }

    @objc private func tick(_ l: CADisplayLink) {
        guard var c = current, let g = goal else { cancel(); return }
        let t = l.targetTimestamp
        let dt = Float(min(1.0 / 30, max(1.0 / 240, t - (last ?? (t - 1.0 / 60)))))
        last = t
        let w = omega
        func spring(_ x: inout Float, _ v: inout Float, _ to: Float) {
            let a = w * w * (to - x) - 2 * w * v
            v += a * dt
            x += v * dt
        }
        spring(&c.azimuth, &velocity.azimuth, g.azimuth)
        spring(&c.elevation, &velocity.elevation, g.elevation)
        spring(&c.distance, &velocity.distance, g.distance)
        let at = w * w * (g.target - c.target) - 2 * w * velocity.target
        velocity.target += at * dt
        c.target += velocity.target * dt
        c.elevation = max(-Self.maxElevation, min(Self.maxElevation, c.elevation))
        current = c
        camera.simdWorldPosition = c.eye
        camera.simdLook(at: c.target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
        let remaining = gap(c, g)
        if !arrived, 1 - remaining / startGap >= Float(Motion.Scene.ringStart) {
            arrived = true
            onArrive?()
        }
        let still = abs(velocity.azimuth) + abs(velocity.elevation) + abs(velocity.distance) / scale
            + simd_length(velocity.target) / scale
        if remaining < 0.002, still < 0.01 {
            camera.simdWorldPosition = g.eye
            camera.simdLook(at: g.target, up: SIMD3(0, 1, 0), localFront: SIMD3(0, 0, -1))
            view?.defaultCameraController.target = SCNVector3(g.target)
            if !arrived { arrived = true; onArrive?() }
            link?.invalidate()
            link = nil
            last = nil
            onSettle?()
        }
    }
}
