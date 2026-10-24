import ARKit
import Metal
import MetalKit
import simd

/// Draws the camera image and the accumulated LiDAR point cloud (PointCloud.metal) every display frame, and folds
/// new depth samples into a GPU voxel hash whenever the camera has moved a little: one point per voxel (2.5 mm
/// inside the locked box, 1 cm elsewhere), replaced only by a better observation, never aged out.
@MainActor
final class PointCloudRenderer: NSObject, MTKViewDelegate {
    /// Hash slots (a power of two): one per voxel; ~40 MB with keys and scores.
    static let maxPoints = 1 << 20
    static let fineCell: Float = 0.0025
    static let coarseCell: Float = 0.01
    static let cellsPerAxis: Float = 1024
    static let depthStep: UInt32 = 2
    static let minConfidence: UInt32 = 1
    static let maxDepthMM: UInt32 = 2000

    struct Particle { var positionConf: SIMD4<Float>; var color: SIMD4<Float> }
    struct BoxUniforms { var center: SIMD4<Float>; var halfSize: SIMD4<Float> }
    struct GridUniforms { var fine: SIMD4<Float>; var coarse: SIMD4<Float> }
    struct FinishUniforms { var params: SIMD4<Float> }
    struct MarkerUniforms { var point: SIMD4<Float>; var params: SIMD4<Float> }

    /// The close-up target to draw (world point), set at ≤ 10 Hz; nil hides it.
    var marker: SIMD3<Float>?
    /// When the "Done" moment started (CACurrentMediaTime), nil while live.
    private var finishStart: CFTimeInterval?
    static let finishDuration: CFTimeInterval = 0.9

    /// Starts the "Done" moment: the cloud gathers into the locked box over `finishDuration`.
    func beginFinish() {
        guard finishStart == nil else { return }
        finishStart = CACurrentMediaTime()
        marker = nil
    }

    let device: MTLDevice
    private let queue: MTLCommandQueue
    private let accumulate: MTLComputePipelineState
    private let particlePipeline: MTLRenderPipelineState
    private let cameraPipeline: MTLRenderPipelineState
    private let markerPipeline: MTLRenderPipelineState
    private let depthOn: MTLDepthStencilState
    private let depthOff: MTLDepthStencilState
    private var cache: CVMetalTextureCache?
    let particles: MTLBuffer
    let counter: MTLBuffer
    let keys: MTLBuffer
    let scores: MTLBuffer
    private(set) var grid = GridUniforms(fine: .zero, coarse: .zero)
    /// "Turn it" (Capture/Handheld): fusion in the object frame and drawing where the object is now, when set.
    var handheld: HandheldFusion?
    private var coarseSet = false
    private var fineSet = false
    private weak var session: ARSession?

    private var lastAccumulated: simd_float4x4?
    private var lastFrameTime: CFTimeInterval?
    private(set) var frameTimes: [Double] = []
    private var held: [CVMetalTexture] = []

    init?(session: ARSession) {
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue(),
              let library = device.makeDefaultLibrary(),
              let kernel = library.makeFunction(name: "accumulatePoints"),
              let accumulate = try? device.makeComputePipelineState(function: kernel),
              let particles = device.makeBuffer(length: MemoryLayout<Particle>.stride * Self.maxPoints, options: .storageModeShared),
              let counter = device.makeBuffer(length: 4, options: .storageModeShared),
              let keys = device.makeBuffer(length: 4 * Self.maxPoints, options: .storageModeShared),
              let scores = device.makeBuffer(length: 4 * Self.maxPoints, options: .storageModeShared) else { return nil }
        memset(keys.contents(), 0xFF, keys.length)      // every slot empty
        memset(scores.contents(), 0, scores.length)
        self.keys = keys
        self.scores = scores
        self.device = device
        self.queue = queue
        self.accumulate = accumulate
        self.particles = particles
        self.counter = counter
        self.session = session
        let empty = particles.contents().bindMemory(to: Particle.self, capacity: Self.maxPoints)
        for i in 0..<Self.maxPoints { empty[i] = Particle(positionConf: SIMD4(0, 0, 0, -1), color: .zero) }
        counter.contents().storeBytes(of: UInt32(0), as: UInt32.self)

        func pipeline(_ v: String, _ f: String, blend: Bool) -> MTLRenderPipelineState? {
            let d = MTLRenderPipelineDescriptor()
            d.vertexFunction = library.makeFunction(name: v)
            d.fragmentFunction = library.makeFunction(name: f)
            d.colorAttachments[0].pixelFormat = .bgra8Unorm
            d.depthAttachmentPixelFormat = .depth32Float
            if blend {
                d.colorAttachments[0].isBlendingEnabled = true
                d.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
                d.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
            }
            return try? device.makeRenderPipelineState(descriptor: d)
        }
        guard let pp = pipeline("particleVertex", "particleFragment", blend: true),
              let cp = pipeline("cameraVertex", "cameraFragment", blend: false),
              let mp = pipeline("markerVertex", "markerFragment", blend: true) else { return nil }
        particlePipeline = pp
        cameraPipeline = cp
        markerPipeline = mp
        let on = MTLDepthStencilDescriptor()
        on.depthCompareFunction = .less
        on.isDepthWriteEnabled = true
        let off = MTLDepthStencilDescriptor()
        off.depthCompareFunction = .always
        off.isDepthWriteEnabled = false
        guard let dOn = device.makeDepthStencilState(descriptor: on), let dOff = device.makeDepthStencilState(descriptor: off) else { return nil }
        depthOn = dOn
        depthOff = dOff
        super.init()
        CVMetalTextureCacheCreate(nil, nil, device, nil, &cache)
    }

    /// Voxels holding a point.
    var appended: Int { Int(counter.contents().load(as: UInt32.self)) }

    /// The stored points (confidence ≥ `minConfidence`), optionally only those inside `box`.
    func points(in box: ObjectBox? = nil, margin: Float = 0) -> [(SIMD3<Float>, SIMD3<UInt8>)] {
        let p = particles.contents().bindMemory(to: Particle.self, capacity: Self.maxPoints)
        var out: [(SIMD3<Float>, SIMD3<UInt8>)] = []
        out.reserveCapacity(appended)
        for i in 0..<Self.maxPoints {
            let q = p[i]
            guard q.positionConf.w >= Float(Self.minConfidence) else { continue }
            let pos = SIMD3(q.positionConf.x, q.positionConf.y, q.positionConf.z)
            if let box, !box.contains(pos, margin: margin) { continue }
            out.append((pos, SIMD3(UInt8(q.color.x * 255), UInt8(q.color.y * 255), UInt8(q.color.z * 255))))
        }
        return out
    }

    /// The locked box. The fine grid is anchored at the first lock (±1.28 m around it) and kept for re-locks, so
    /// voxels stay the same cells and keep de-duplicating.
    var box: ObjectBox? {
        didSet {
            guard let box, !fineSet else { return }
            fineSet = true
            grid.fine = SIMD4(box.center - Self.fineCell * Self.cellsPerAxis / 2, Self.fineCell)
        }
    }

    /// Puts earlier points (a continued scan's cloud, already in this world frame) into the hash as fine voxels with
    /// the lowest quality, so any new observation of the same spot replaces them. Call after `box` is set.
    /// `confidence` < 1: drawn but not exported ("Scan the bottom" shows the main scan's cloud lined up with it).
    func seed(_ pts: [(SIMD3<Float>, SIMD3<UInt8>)], confidence: Float = 2) -> Int {
        guard fineSet else { return 0 }
        let keyBuf = keys.contents().bindMemory(to: UInt32.self, capacity: Self.maxPoints)
        let scoreBuf = scores.contents().bindMemory(to: UInt32.self, capacity: Self.maxPoints)
        let part = particles.contents().bindMemory(to: Particle.self, capacity: Self.maxPoints)
        let cap = UInt32(Self.maxPoints)
        var added: UInt32 = 0
        for (p, c) in pts {
            let cell = ((p - SIMD3(grid.fine.x, grid.fine.y, grid.fine.z)) / grid.fine.w).rounded(.down)
            guard cell.min() >= 0, cell.max() < Self.cellsPerAxis else { continue }
            let key = (UInt32(1) << 30) | (UInt32(cell.x) << 20) | (UInt32(cell.y) << 10) | UInt32(cell.z)
            var slot = (key &* 2654435761) & (cap - 1)
            for _ in 0..<32 {
                if keyBuf[Int(slot)] == 0xFFFF_FFFF {
                    keyBuf[Int(slot)] = key
                    scoreBuf[Int(slot)] = (2 << 24) | 1
                    part[Int(slot)] = Particle(positionConf: SIMD4(p, confidence), color: SIMD4(SIMD3<Float>(c) / 255, 1))
                    added += 1
                    break
                }
                if keyBuf[Int(slot)] == key { break }
                slot = (slot + 1) & (cap - 1)
            }
        }
        let count = counter.contents().load(as: UInt32.self)
        counter.contents().storeBytes(of: count + added, as: UInt32.self)
        return Int(added)
    }

    func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    func draw(in view: MTKView) {
        let now = CACurrentMediaTime()
        if let last = lastFrameTime {
            frameTimes.append((now - last) * 1000)
            if frameTimes.count > 600 { frameTimes.removeFirst(frameTimes.count - 600) }
        }
        lastFrameTime = now
        guard let frame = session?.currentFrame, let cache,
              let pass = view.currentRenderPassDescriptor, let drawable = view.currentDrawable,
              let cmd = queue.makeCommandBuffer() else { return }
        let image = frame.capturedImage
        held = []
        guard let yTex = texture(image, .r8Unorm, plane: 0, cache: cache),
              let cTex = texture(image, .rg8Unorm, plane: 1, cache: cache) else { return }
        var boxU = boxUniforms()

        // Accumulate when the camera moved ≥ 1 cm or turned ≥ 1.5° (or on the first frame).
        if let handheld, let box {
            handheld.encodeFusion(cmd, renderer: self, box: box)
        } else if let depth = frame.smoothedSceneDepth ?? frame.sceneDepth, let conf = depth.confidenceMap,
           moved(frame.camera.transform),
           let dTex = texture(depth.depthMap, .r32Float, plane: 0, cache: cache),
           let kTex = texture(conf, .r8Unorm, plane: 0, cache: cache),
           let enc = cmd.makeComputeCommandEncoder() {
            lastAccumulated = frame.camera.transform
            if !coarseSet {
                coarseSet = true
                let c = frame.camera.transform.columns.3
                grid.coarse = SIMD4(SIMD3(c.x, c.y, c.z) - Self.coarseCell * Self.cellsPerAxis / 2, Self.coarseCell)
            }
            var toWorld = frame.camera.transform
            var k = frame.camera.intrinsics
            let sx = Float(dTex.width) / Float(CVPixelBufferGetWidth(image)), sy = Float(dTex.height) / Float(CVPixelBufferGetHeight(image))
            k[0][0] *= sx; k[2][0] *= sx; k[1][1] *= sy; k[2][1] *= sy
            var params = SIMD4<UInt32>(Self.depthStep, UInt32(Self.maxPoints), Self.minConfidence, Self.maxDepthMM)
            enc.setComputePipelineState(accumulate)
            enc.setTexture(dTex, index: 0)
            enc.setTexture(kTex, index: 1)
            enc.setTexture(yTex, index: 2)
            enc.setTexture(cTex, index: 3)
            enc.setBytes(&toWorld, length: MemoryLayout<simd_float4x4>.stride, index: 0)
            enc.setBytes(&k, length: MemoryLayout<simd_float3x3>.stride, index: 1)
            enc.setBytes(&params, length: MemoryLayout<SIMD4<UInt32>>.stride, index: 2)
            enc.setBytes(&boxU, length: MemoryLayout<BoxUniforms>.stride, index: 3)
            enc.setBuffer(particles, offset: 0, index: 4)
            enc.setBuffer(counter, offset: 0, index: 5)
            enc.setBuffer(keys, offset: 0, index: 6)
            enc.setBuffer(scores, offset: 0, index: 7)
            enc.setBytes(&grid, length: MemoryLayout<GridUniforms>.stride, index: 8)
            let grid = MTLSize(width: (dTex.width + 1) / Int(Self.depthStep), height: (dTex.height + 1) / Int(Self.depthStep), depth: 1)
            enc.dispatchThreads(grid, threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
            enc.endEncoding()
        }

        guard let enc = cmd.makeRenderCommandEncoder(descriptor: pass) else { return }
        // Camera image: the quad's texture coordinates follow ARKit's display transform for this view.
        let size = view.bounds.size
        let inv = frame.displayTransform(for: .portrait, viewportSize: size).inverted()
        func uv(_ x: CGFloat, _ y: CGFloat) -> SIMD2<Float> {
            let p = CGPoint(x: x, y: y).applying(inv)
            return SIMD2(Float(p.x), Float(p.y))
        }
        var quad: [SIMD4<Float>] = [
            SIMD4(-1, -1, uv(0, 1).x, uv(0, 1).y), SIMD4(1, -1, uv(1, 1).x, uv(1, 1).y),
            SIMD4(-1, 1, uv(0, 0).x, uv(0, 0).y), SIMD4(1, 1, uv(1, 0).x, uv(1, 0).y),
        ]
        let progress = finishStart.map { Float(min(1, (now - $0) / Self.finishDuration)) } ?? 0
        var finish = FinishUniforms(params: SIMD4(progress, 0, 0, 0))
        enc.setRenderPipelineState(cameraPipeline)
        enc.setDepthStencilState(depthOff)
        enc.setVertexBytes(&quad, length: MemoryLayout<SIMD4<Float>>.stride * 4, index: 0)
        enc.setFragmentTexture(yTex, index: 0)
        enc.setFragmentTexture(cTex, index: 1)
        enc.setFragmentBytes(&finish, length: MemoryLayout<FinishUniforms>.stride, index: 0)
        enc.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)

        var vp = frame.camera.projectionMatrix(for: .portrait, viewportSize: size, zNear: 0.01, zFar: 10)
            * frame.camera.viewMatrix(for: .portrait)
        var scale = Float(view.drawableSize.height) * 0.006
        var objectToWorld = handheld?.objectToWorld(frame.timestamp) ?? matrix_identity_float4x4
        enc.setRenderPipelineState(handheld?.particlePipeline ?? particlePipeline)
        enc.setVertexBytes(&objectToWorld, length: MemoryLayout<simd_float4x4>.stride, index: 5)
        enc.setDepthStencilState(depthOn)
        enc.setVertexBuffer(particles, offset: 0, index: 0)
        enc.setVertexBytes(&vp, length: MemoryLayout<simd_float4x4>.stride, index: 1)
        enc.setVertexBytes(&boxU, length: MemoryLayout<BoxUniforms>.stride, index: 2)
        enc.setVertexBytes(&scale, length: MemoryLayout<Float>.stride, index: 3)
        enc.setVertexBytes(&finish, length: MemoryLayout<FinishUniforms>.stride, index: 4)
        // Every slot: empty ones are dropped in the vertex shader.
        enc.drawPrimitives(type: .point, vertexStart: 0, vertexCount: Self.maxPoints)
        if let marker {
            let px = Float(view.drawableSize.width) * 0.2
            var mu = MarkerUniforms(point: SIMD4(marker, 1),
                                    params: SIMD4(Float(now.truncatingRemainder(dividingBy: 1000)), px,
                                                  Float(view.drawableSize.width), Float(view.drawableSize.height)))
            enc.setRenderPipelineState(markerPipeline)
            enc.setDepthStencilState(depthOff)
            var mvp = vp * objectToWorld   // the marker is on the object (object frame while turning it)
            enc.setVertexBytes(&mvp, length: MemoryLayout<simd_float4x4>.stride, index: 0)
            enc.setVertexBytes(&mu, length: MemoryLayout<MarkerUniforms>.stride, index: 1)
            enc.setFragmentBytes(&mu, length: MemoryLayout<MarkerUniforms>.stride, index: 0)
            enc.drawPrimitives(type: .triangleStrip, vertexStart: 0, vertexCount: 4)
        }
        enc.endEncoding()
        cmd.present(drawable)
        // The Metal textures wrap ARKit's buffers: keep them until the GPU is done with this frame.
        let keep = Unchecked(value: held)
        cmd.addCompletedHandler { _ in _ = keep }
        cmd.commit()
    }

    private func boxUniforms() -> BoxUniforms {
        guard let b = box else { return BoxUniforms(center: .zero, halfSize: .zero) }
        return BoxUniforms(center: SIMD4(b.center, 1), halfSize: SIMD4(b.size / 2, b.yaw))
    }

    private func moved(_ m: simd_float4x4) -> Bool {
        guard let last = lastAccumulated else { return true }
        let d = simd_distance(SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z), SIMD3(last.columns.3.x, last.columns.3.y, last.columns.3.z))
        let fa = -SIMD3(last.columns.2.x, last.columns.2.y, last.columns.2.z), fb = -SIMD3(m.columns.2.x, m.columns.2.y, m.columns.2.z)
        return d >= 0.01 || acos(max(-1, min(1, simd_dot(fa, fb)))) >= 1.5 * .pi / 180
    }

    private func texture(_ buffer: CVPixelBuffer, _ format: MTLPixelFormat, plane: Int, cache: CVMetalTextureCache) -> MTLTexture? {
        let w = CVPixelBufferGetWidthOfPlane(buffer, plane), h = CVPixelBufferGetHeightOfPlane(buffer, plane)
        let isPlanar = CVPixelBufferGetPlaneCount(buffer) > 0
        var tex: CVMetalTexture?
        let status = CVMetalTextureCacheCreateTextureFromImage(nil, cache, buffer, nil, format,
                                                               isPlanar ? w : CVPixelBufferGetWidth(buffer),
                                                               isPlanar ? h : CVPixelBufferGetHeight(buffer),
                                                               isPlanar ? plane : 0, &tex)
        guard status == kCVReturnSuccess, let tex else { return nil }
        held.append(tex)
        return CVMetalTextureGetTexture(tex)
    }
}
