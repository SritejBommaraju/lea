import Foundation
import Metal
import MetalKit
import SceneKit
import simd

// The gateway's photo-real tier: a Gaussian splat (backend repairos.recreation.splat, `.splat` file, 32 bytes per
// Gaussian, the scan's ARKit world frame), drawn by MetalSplatter (MIT, vendored in ThirdParty/MetalSplatter) in a
// Metal view under the recreation's SceneKit view. SceneKit still owns the camera (orbit, focus fly-to), the turntable
// and the numbered markers; every frame the splat is drawn with SceneKit's own camera and the content's turntable
// transform, so splat, markers and findings share one frame. Any failure (no Metal device, an unreadable file, the
// renderer not starting) returns nil and the caller falls back to the textured mesh.

/// A decoded `.splat` (antimatter15 layout: float32 x y z, float32 scale x y z (linear), uint8 r g b opacity,
/// uint8 quaternion w x y z as q * 128 + 128).
struct SplatFile: Sendable {
    static let stride = 32
    var points: [SplatPoint]
    var lo: SIMD3<Float>
    var hi: SIMD3<Float>

    enum Failure: Error { case empty, truncated(Int) }

    /// Decodes the file; with `fadeCenter`, Gaussians fade out horizontally between `fadeInner` and `fadeOuter` of
    /// it (the table patch around the object, as the textured mesh's shader does).
    static func decode(_ data: Data, fadeCenter: SIMD3<Float>? = nil, fadeInner: Float = 0, fadeOuter: Float = 0) throws -> SplatFile {
        guard !data.isEmpty else { throw Failure.empty }
        guard data.count % stride == 0 else { throw Failure.truncated(data.count) }
        let n = data.count / stride
        var points: [SplatPoint] = []
        points.reserveCapacity(n)
        var lo = SIMD3<Float>(repeating: .infinity), hi = -lo
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            for i in 0..<n {
                let o = i * stride
                func f(_ k: Int) -> Float { Float(bitPattern: UInt32(littleEndian: raw.loadUnaligned(fromByteOffset: o + 4 * k, as: UInt32.self))) }
                func b(_ k: Int) -> UInt8 { raw[o + 24 + k] }
                let p = SIMD3(f(0), f(1), f(2))
                guard p.x.isFinite, p.y.isFinite, p.z.isFinite else { continue }
                var alpha = b(3)
                if let c = fadeCenter, fadeOuter > fadeInner {
                    let d = simd_length(SIMD2(p.x - c.x, p.z - c.z))
                    let keep = 1 - min(1, max(0, (d - fadeInner) / (fadeOuter - fadeInner)))
                    if keep <= 0 { continue }
                    alpha = UInt8(Float(alpha) * keep)
                }
                let q = simd_quatf(ix: Float(b(5)) - 128, iy: Float(b(6)) - 128, iz: Float(b(7)) - 128, r: Float(b(4)) - 128)
                points.append(SplatPoint(position: p, color: .sRGBUInt8(SIMD3(b(0), b(1), b(2))),
                                         opacity: .linearUInt8(alpha), scale: .linearFloat(SIMD3(f(3), f(4), f(5))),
                                         rotation: simd_length(q.vector) > 0 ? q.normalized : simd_quatf(ix: 0, iy: 0, iz: 0, r: 1)))
                lo = simd_min(lo, p); hi = simd_max(hi, p)
            }
        }
        guard !points.isEmpty else { throw Failure.empty }
        return SplatFile(points: points, lo: lo, hi: hi)
    }

    /// Up to `limit` Gaussian centres (evenly spread through the file), for putting markers on the surface.
    func centres(limit: Int = 20_000) -> [SIMD3<Float>] {
        let step = max(1, points.count / limit)
        return Swift.stride(from: 0, to: points.count, by: step).map { points[$0].position }
    }
}

/// A splat ready to draw: the renderer with the scene loaded, and the centres for marker snapping.
final class SplatScene: @unchecked Sendable {
    static let colorFormat: MTLPixelFormat = .bgra8Unorm_srgb
    static let depthFormat: MTLPixelFormat = .depth32Float

    let device: MTLDevice
    let renderer: SplatRenderer
    let count: Int
    let surface: [SIMD3<Float>]
    let lo: SIMD3<Float>
    let hi: SIMD3<Float>

    private init(device: MTLDevice, renderer: SplatRenderer, file: SplatFile) {
        self.device = device
        self.renderer = renderer
        self.count = file.points.count
        self.surface = file.centres()
        self.lo = file.lo
        self.hi = file.hi
    }

    /// The renderer's shaders are in the app's default library (ThirdParty/MetalSplatter/*.metal).
    static func shadersPresent(_ device: MTLDevice) -> Bool {
        guard let lib = try? device.makeDefaultLibrary(bundle: .main) else { return false }
        return ["singleStageSplatVertexShader", "singleStageSplatFragmentShader"].allSatisfy { lib.makeFunction(name: $0) != nil }
    }

    /// Loads a `.splat` file and its renderer; nil on any failure (the caller keeps the textured mesh).
    static func load(_ data: Data, focus: (center: SIMD3<Float>, radius: Float)?) async -> SplatScene? {
        guard let device = MTLCreateSystemDefaultDevice(), shadersPresent(device) else { return nil }
        do {
            let r = focus.map { max(0.02, $0.radius) }
            let file = try SplatFile.decode(data, fadeCenter: focus?.center, fadeInner: (r ?? 0) * 1.3, fadeOuter: (r ?? 0) * 2.2)
            let renderer = try SplatRenderer(device: device, colorFormat: colorFormat, depthFormat: depthFormat,
                                             sampleCount: 1, maxViewCount: 1, maxSimultaneousRenders: 3)
            let chunk = try SplatChunk(device: device, from: file.points)
            _ = await renderer.addChunk(chunk)
            return SplatScene(device: device, renderer: renderer, file: file)
        } catch {
            return nil
        }
    }

    /// Metal-style (depth 0...1) right-handed perspective with SceneKit's field of view and its projection
    /// direction (`fovDegrees` spans the width when `horizontal`, else the height).
    static func projection(fovDegrees: Float, horizontal: Bool, aspect: Float, near: Float, far: Float) -> simd_float4x4 {
        let half = fovDegrees * .pi / 360
        let ys: Float = horizontal ? aspect / tan(half) : 1 / tan(half)
        let xs = ys / aspect
        let zs = far / (near - far)
        return simd_float4x4(columns: (SIMD4(xs, 0, 0, 0), SIMD4(0, ys, 0, 0), SIMD4(0, 0, zs, -1), SIMD4(0, 0, zs * near, 0)))
    }

    /// The view matrix for the splat: SceneKit's camera (inverse of its world transform) times the content's
    /// world transform (the turntable pivot), so the splat turns with the markers.
    static func viewMatrix(camera: simd_float4x4, content: simd_float4x4) -> simd_float4x4 {
        camera.inverse * content
    }

    /// Draws one frame into `color`/`depth` with the stage's current camera; false when nothing was drawn.
    @discardableResult
    func draw(stage: StageCamera, size: CGSize, color: MTLTexture, depth: MTLTexture?, to commandBuffer: MTLCommandBuffer) throws -> Bool {
        let aspect = Float(max(size.width, 1) / max(size.height, 1))
        let proj = Self.projection(fovDegrees: stage.fieldOfView, horizontal: stage.horizontal, aspect: aspect,
                                   near: stage.near, far: stage.far)
        let vp = SplatRenderer.ViewportDescriptor(
            viewport: MTLViewport(originX: 0, originY: 0, width: Double(size.width), height: Double(size.height), znear: 0, zfar: 1),
            projectionMatrix: proj, viewMatrix: Self.viewMatrix(camera: stage.camera, content: stage.content),
            screenSize: SIMD2(Int(size.width), Int(size.height)))
        return try renderer.render(viewports: [vp], colorTexture: color, colorStoreAction: .store, depthTexture: depth,
                                   rasterizationRateMap: nil, renderTargetArrayLength: 0, to: commandBuffer)
    }
}

/// What the splat needs from SceneKit each frame (read on the main thread from the presentation nodes).
struct StageCamera: Sendable {
    var camera: simd_float4x4
    var content: simd_float4x4
    var fieldOfView: Float
    var horizontal: Bool
    var near: Float
    var far: Float

    @MainActor
    init(cameraNode: SCNNode, content: SCNNode) {
        let cam = cameraNode.presentation
        camera = cam.simdWorldTransform
        self.content = content.presentation.simdWorldTransform
        fieldOfView = Float(cam.camera?.fieldOfView ?? 30)
        horizontal = cam.camera?.projectionDirection == .horizontal
        near = Float(cam.camera?.zNear ?? 0.005)
        far = Float(cam.camera?.zFar ?? 20)
    }

    init(camera: simd_float4x4, content: simd_float4x4, fieldOfView: Float, horizontal: Bool, near: Float, far: Float) {
        self.camera = camera
        self.content = content
        self.fieldOfView = fieldOfView
        self.horizontal = horizontal
        self.near = near
        self.far = far
    }
}

/// The Metal view the splat is drawn in, under the transparent SceneKit view. Draws every display frame while
/// active; reports a render failure once (the recreation then falls back to the textured mesh).
#if canImport(UIKit)
final class SplatMetalView: MTKView, MTKViewDelegate {
    let splat: SplatScene
    weak var stage: RecreationStage?
    var onFailure: (() -> Void)?
    private let queue: MTLCommandQueue
    private var failed = false
    private(set) var frameTimes: [CFTimeInterval] = []

    init?(splat: SplatScene, stage: RecreationStage) {
        guard let q = splat.device.makeCommandQueue() else { return nil }
        self.splat = splat
        self.stage = stage
        self.queue = q
        super.init(frame: .zero, device: splat.device)
        colorPixelFormat = SplatScene.colorFormat
        depthStencilPixelFormat = SplatScene.depthFormat
        sampleCount = 1
        clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 0)
        isOpaque = false
        backgroundColor = .clear
        layer.isOpaque = false
        preferredFramesPerSecond = 60
        isUserInteractionEnabled = false
        delegate = self
    }

    required init(coder: NSCoder) { fatalError("not used") }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        guard !failed, let stage, let drawable = currentDrawable, let cb = queue.makeCommandBuffer() else { return }
        let t0 = CACurrentMediaTime()
        let cam = StageCamera(cameraNode: stage.cameraNode, content: stage.content)
        do {
            if try splat.draw(stage: cam, size: drawableSize, color: drawable.texture, depth: depthStencilTexture, to: cb) {
                cb.present(drawable)
            }
            cb.commit()
        } catch {
            failed = true
            isPaused = true
            onFailure?()
            return
        }
        frameTimes.append(CACurrentMediaTime() - t0)
        if frameTimes.count > 240 { frameTimes.removeFirst(frameTimes.count - 240) }
    }
}
#endif
