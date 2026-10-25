import Metal
import simd

/// GPU side of the hand-held scan: fuses tracked frames into the renderer's voxel hash in the OBJECT frame (one
/// dispatch per tracked frame, at the tracker's rate, with that frame's own pose, so the cloud never smears when the
/// object turns), and draws the in-box points where the object is now (`objectToWorld` extrapolated to the display
/// frame's time). The renderer calls `encodeFusion` instead of its world-frame accumulation while this is set.
@MainActor
final class HandheldFusion {
    struct Uniforms {
        var cameraToWorld: simd_float4x4
        var cameraToObject: simd_float4x4
        var intrinsics: SIMD4<Float>
        var plane: SIMD4<Float>
        var params: SIMD4<UInt32>
    }

    /// One tracked frame to fuse: LiDAR depth + confidence, colour and person mask at depth resolution.
    struct Pending {
        var width: Int, height: Int
        var depth: [Float]
        var confidence: [UInt8]
        var rgba: [UInt8]
        var mask: [UInt8]
        var uniforms: Uniforms
    }

    let particlePipeline: MTLRenderPipelineState
    private let kernel: MTLComputePipelineState
    /// Two sets, alternated: the GPU may still read the last frame's while this one is written.
    private var textures: [(depth: MTLTexture, conf: MTLTexture, color: MTLTexture, mask: MTLTexture)] = []
    private var flip = 0
    private var pending: Pending?
    /// Display pose for a frame time (the controller's extrapolation).
    var objectToWorld: (Double) -> simd_float4x4 = { _ in matrix_identity_float4x4 }
    private(set) var fused = 0

    init?(device: MTLDevice) {
        guard let lib = device.makeDefaultLibrary(), let fn = lib.makeFunction(name: "accumulateObjectPoints"),
              let k = try? device.makeComputePipelineState(function: fn) else { return nil }
        let d = MTLRenderPipelineDescriptor()
        d.vertexFunction = lib.makeFunction(name: "particleVertexObject")
        d.fragmentFunction = lib.makeFunction(name: "particleFragment")
        d.colorAttachments[0].pixelFormat = .bgra8Unorm
        d.depthAttachmentPixelFormat = .depth32Float
        d.colorAttachments[0].isBlendingEnabled = true
        d.colorAttachments[0].sourceRGBBlendFactor = .sourceAlpha
        d.colorAttachments[0].destinationRGBBlendFactor = .oneMinusSourceAlpha
        guard let pp = try? device.makeRenderPipelineState(descriptor: d) else { return nil }
        kernel = k
        particlePipeline = pp
    }

    func submit(_ p: Pending) { pending = p }

    /// Before the object first moves: drop what is not the object from the hash, so nothing of the table or the
    /// room rides along with it (points under the plane clearance inside the box, coarse points around it).
    func prepare(renderer r: PointCloudRenderer, box: ObjectBox, planeY: Float?, clearance: Float) -> Int {
        let part = r.particles.contents().bindMemory(to: PointCloudRenderer.Particle.self, capacity: PointCloudRenderer.maxPoints)
        let scores = r.scores.contents().bindMemory(to: UInt32.self, capacity: PointCloudRenderer.maxPoints)
        var removed = 0
        for i in 0..<PointCloudRenderer.maxPoints where part[i].positionConf.w >= 0 {
            let p = SIMD3(part[i].positionConf.x, part[i].positionConf.y, part[i].positionConf.z)
            guard box.contains(p, margin: 0.012) else { continue }
            let table = planeY.map { p.y < $0 + clearance } ?? false
            if table || !box.contains(p, margin: 0.002) {
                part[i].positionConf.w = -1
                scores[i] = 0
                removed += 1
            }
        }
        return removed
    }

    func encodeFusion(_ cmd: MTLCommandBuffer, renderer r: PointCloudRenderer, box: ObjectBox) {
        guard let p = pending else { return }
        pending = nil
        let device = r.device
        if textures.count != 2 || textures[0].depth.width != p.width || textures[0].depth.height != p.height {
            func tex(_ f: MTLPixelFormat) -> MTLTexture? {
                let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: f, width: p.width, height: p.height, mipmapped: false)
                d.usage = .shaderRead
                d.storageMode = .shared
                return device.makeTexture(descriptor: d)
            }
            textures = []
            for _ in 0..<2 {
                guard let a = tex(.r32Float), let b = tex(.r8Unorm), let c = tex(.rgba8Unorm), let m = tex(.r8Unorm) else { return }
                textures.append((a, b, c, m))
            }
        }
        flip ^= 1
        let t = textures[flip]
        guard let enc = cmd.makeComputeCommandEncoder() else { return }
        let region = MTLRegionMake2D(0, 0, p.width, p.height)
        p.depth.withUnsafeBytes { t.depth.replace(region: region, mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: p.width * 4) }
        // ARKit confidence 0/1/2 as r8Unorm: the kernel reads ×255.
        p.confidence.withUnsafeBytes { t.conf.replace(region: region, mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: p.width) }
        p.rgba.withUnsafeBytes { t.color.replace(region: region, mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: p.width * 4) }
        p.mask.withUnsafeBytes { t.mask.replace(region: region, mipmapLevel: 0, withBytes: $0.baseAddress!, bytesPerRow: p.width) }
        var u = p.uniforms
        var b = PointCloudRenderer.BoxUniforms(center: SIMD4(box.center, 1), halfSize: SIMD4(box.size / 2, box.yaw))
        var grid = r.grid
        enc.setComputePipelineState(kernel)
        enc.setTexture(t.depth, index: 0)
        enc.setTexture(t.conf, index: 1)
        enc.setTexture(t.color, index: 2)
        enc.setTexture(t.mask, index: 3)
        enc.setBytes(&u, length: MemoryLayout<Uniforms>.stride, index: 0)
        enc.setBytes(&b, length: MemoryLayout<PointCloudRenderer.BoxUniforms>.stride, index: 1)
        enc.setBuffer(r.particles, offset: 0, index: 2)
        enc.setBuffer(r.counter, offset: 0, index: 3)
        enc.setBuffer(r.keys, offset: 0, index: 4)
        enc.setBuffer(r.scores, offset: 0, index: 5)
        enc.setBytes(&grid, length: MemoryLayout<PointCloudRenderer.GridUniforms>.stride, index: 6)
        let step = Int(u.params.x)
        enc.dispatchThreads(MTLSize(width: (p.width + step - 1) / step, height: (p.height + step - 1) / step, depth: 1),
                            threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
        enc.endEncoding()
        fused += 1
    }
}
