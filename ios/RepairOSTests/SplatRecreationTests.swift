import Foundation
import ImageIO
import Metal
import SceneKit
import Testing
import simd
@testable import RepairOS

/// The gateway's splat tier (Report/Recreation/RecreationSplat + the vendored MetalSplatter): the backend's `.splat`
/// file decodes as written (Fixtures/backend-cube.splat was written by backend repairos.recreation.splat.splat_bytes),
/// the splat is drawn with SceneKit's camera in the scan frame (a Gaussian lands on the pixel SceneKit projects its
/// point to), a broken splat falls back to the textured mesh, and a 300k-Gaussian frame is timed.
@MainActor
struct SplatRecreationTests {
    static func fixture() throws -> Data {
        let here = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        return try Data(contentsOf: here.appending(path: "Fixtures/backend-cube.splat"))
    }

    /// `.splat` bytes as the backend writes them (float32 x y z, float32 scales, uint8 rgba, uint8 quaternion w x y z).
    static func splatBytes(_ pts: [(p: SIMD3<Float>, s: Float, rgb: SIMD3<UInt8>, a: UInt8)]) -> Data {
        var d = Data()
        for g in pts {
            for f in [g.p.x, g.p.y, g.p.z, g.s, g.s, g.s] { withUnsafeBytes(of: f.bitPattern.littleEndian) { d.append(contentsOf: $0) } }
            d.append(contentsOf: [g.rgb.x, g.rgb.y, g.rgb.z, g.a, 255, 128, 128, 128])
        }
        return d
    }

    @Test func decodesTheBackendWriter() throws {
        let f = try SplatFile.decode(Self.fixture())
        #expect(f.points.count == 600)
        #expect(simd_distance(f.lo, SIMD3(-0.05, 0, -0.05)) < 0.002 && simd_distance(f.hi, SIMD3(0.05, 0.1, 0.05)) < 0.002)
        let first = f.points[0]   // read back on the backend: (0.0141, 0.1, 0.0146), rgb (0.553, 0.557, 0.8)
        #expect(simd_distance(first.position, SIMD3(0.01412291, 0.1, 0.01459284)) < 1e-6)
        let rgb = first.color.asSRGBFloat
        #expect(abs(rgb.x - 0.5529) < 0.003 && abs(rgb.z - 0.8) < 0.003)
        #expect(abs(first.opacity.asLinearFloat - 229.0 / 255) < 0.01)
        #expect(simd_distance(first.scale.asLinearFloat, SIMD3(repeating: 0.004)) < 1e-6)
        #expect(abs(first.rotation.real - 1) < 0.01)
        #expect(f.centres(limit: 100).count == 100)
    }

    @Test func fadesTheTablePatchAndRejectsBrokenFiles() throws {
        let near = (p: SIMD3<Float>(0.01, 0, 0), s: Float(0.01), rgb: SIMD3<UInt8>(200, 10, 10), a: UInt8(255))
        let far = (p: SIMD3<Float>(0.5, 0, 0), s: Float(0.01), rgb: SIMD3<UInt8>(10, 200, 10), a: UInt8(255))
        let mid = (p: SIMD3<Float>(0.175, 0, 0), s: Float(0.01), rgb: SIMD3<UInt8>(10, 10, 200), a: UInt8(255))
        let f = try SplatFile.decode(Self.splatBytes([near, far, mid]), fadeCenter: .zero, fadeInner: 0.13, fadeOuter: 0.22)
        #expect(f.points.count == 2)                                  // the far one is past the fade: dropped
        #expect(f.points[0].opacity.asLinearUInt8 == 255)             // inside: as is
        #expect(f.points[1].opacity.asLinearUInt8 < 160)              // half way through the fade
        #expect(throws: (any Error).self) { try SplatFile.decode(Data([1, 2, 3])) }
        #expect(throws: (any Error).self) { try SplatFile.decode(Data()) }
    }

    @Test func projectionMatchesSceneKitsCamera() {
        for (size, horizontal) in [(CGSize(width: 300, height: 600), true), (CGSize(width: 600, height: 300), false)] {
            let cam = SCNCamera()
            cam.fieldOfView = 30
            cam.zNear = 0.005
            cam.zFar = 20
            cam.projectionDirection = horizontal ? .horizontal : .vertical
            let theirs = simd_float4x4(cam.projectionTransform(withViewportSize: size))
            let ours = SplatScene.projection(fovDegrees: 30, horizontal: horizontal, aspect: Float(size.width / size.height),
                                             near: 0.005, far: 20)
            for p in [SIMD4<Float>(0.03, -0.02, -0.4, 1), SIMD4<Float>(-0.1, 0.05, -1.2, 1)] {
                let a = theirs * p, b = ours * p
                #expect(abs(a.x / a.w - b.x / b.w) < 1e-4 && abs(a.y / a.w - b.y / b.w) < 1e-4)
            }
        }
    }

    /// One bright Gaussian at a world point, the content turned by the turntable: the splat's pixels centre on the
    /// pixel SceneKit's own camera projects that point to (scan frame, Y up, no flip).
    @Test func aGaussianLandsWhereSceneKitProjectsItsPoint() async throws {
        guard let device = MTLCreateSystemDefaultDevice() else { return }
        let world = SIMD3<Float>(0.02, 0.05, -0.01)
        let data = Self.splatBytes([(p: world, s: 0.004, rgb: SIMD3(255, 255, 255), a: 255)])
        guard let scene = await SplatScene.load(data, focus: nil) else {
            Issue.record("the splat did not load")
            return
        }
        let stage = RecreationStage()
        stage.set(.splat(scene), focus: (SIMD3(0, 0.05, 0), 0.08))
        stage.pivot.simdOrientation = simd_quatf(angle: 0.7, axis: SIMD3(0, 1, 0))   // part way round the turntable
        let size = CGSize(width: 240, height: 320)
        stage.cameraNode.camera?.projectionDirection = .horizontal                  // as FittingSCNView sets it
        let cam = StageCamera(camera: stage.cameraNode.simdWorldTransform, content: stage.content.simdWorldTransform,
                              fieldOfView: Float(stage.cameraNode.camera!.fieldOfView), horizontal: true, near: 0.005, far: 20)
        let pixels = try await Self.render(scene, cam, size: size, device: device)
        var sum = SIMD2<Double>(0, 0), n = 0.0
        for y in 0..<Int(size.height) { for x in 0..<Int(size.width) {
            let a = Double(pixels[(y * Int(size.width) + x) * 4 + 3])
            if a > 40 { sum += SIMD2(Double(x) + 0.5, Double(y) + 0.5) * a; n += a }
        } }
        #expect(n > 0)
        guard n > 0 else { return }
        let got = sum / n
        // Expected: SceneKit's projection of the turned point (content world transform, then its camera).
        let proj = simd_float4x4(stage.cameraNode.camera!.projectionTransform(withViewportSize: size))
        let clip = proj * stage.cameraNode.simdWorldTransform.inverse * stage.content.simdWorldTransform * SIMD4(world, 1)
        let ndc = SIMD2(clip.x / clip.w, clip.y / clip.w)
        let want = SIMD2<Double>(Double((ndc.x + 1) / 2) * Double(size.width), Double((1 - ndc.y) / 2) * Double(size.height))
        #expect(simd_distance(got, want) < 2.0, "splat centre \(got), SceneKit projects \(want)")
    }

    @Test func aBrokenSplatFallsBackToTheMesh() async throws {
        #expect(await SplatScene.load(Data(repeating: 7, count: 33), focus: nil) == nil)
        let dir = FileManager.default.temporaryDirectory.appending(path: "splat-fallback-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        func put(_ sub: String, _ m: RecreationManifest, _ body: Data) throws {
            let f = dir.appending(path: sub)
            try FileManager.default.createDirectory(at: f, withIntermediateDirectories: true)
            try body.write(to: f.appending(path: m.file))
            try JSONEncoder().encode(m).write(to: f.appending(path: "recreation.json"))
        }
        try put("recreation/gateway", RecreationManifest(kind: "textured_mesh", origin: "gateway", rev: 3, file: "model.usdz"), Data([0x50, 0x4b]))
        try put("recreation/gateway-splat", RecreationManifest(kind: "splat", origin: "gateway", rev: 1, file: "model.splat"), Data([1, 2, 3]))
        let c = RecreationCenter.candidates(in: dir)
        #expect(c.map(\.manifest.kind) == (RecreationView.metalAvailable ? ["splat", "textured_mesh"] : ["textured_mesh"]))
        #expect(RecreationCenter.best(in: dir)?.manifest.kind == c.first?.manifest.kind)
    }

    /// 300k Gaussians (the backend's cap is 450k, about 14 MB), one 1290 x 2796 frame (iPhone 16 Pro Max, native):
    /// the simulator's GPU is the Mac's, so the time is printed, not judged against 60 fps.
    @Test func timesAThreeHundredThousandGaussianFrame() async throws {
        guard let device = MTLCreateSystemDefaultDevice() else { return }
        var rng = SystemRandomNumberGenerator()
        var pts: [(p: SIMD3<Float>, s: Float, rgb: SIMD3<UInt8>, a: UInt8)] = []
        pts.reserveCapacity(300_000)
        for _ in 0..<300_000 {
            let p = SIMD3<Float>(Float.random(in: -0.08...0.08, using: &rng), Float.random(in: 0...0.16, using: &rng),
                                 Float.random(in: -0.08...0.08, using: &rng))
            pts.append((p: p, s: 0.002, rgb: SIMD3(UInt8.random(in: 0...255, using: &rng), 128, 128), a: 200))
        }
        let t0 = Date()
        guard let scene = await SplatScene.load(Self.splatBytes(pts), focus: (SIMD3(0, 0.08, 0), 0.12)) else {
            Issue.record("the splat did not load")
            return
        }
        let loadS = Date().timeIntervalSince(t0)
        let stage = RecreationStage()
        stage.set(.splat(scene), focus: (SIMD3(0, 0.08, 0), 0.12))
        let cam = StageCamera(cameraNode: stage.cameraNode, content: stage.content)
        let size = CGSize(width: 1290, height: 2796)
        _ = try await Self.render(scene, cam, size: size, device: device)   // warm: first sort, pipelines
        var times: [Double] = []
        for _ in 0..<10 {
            let t = Date()
            _ = try await Self.render(scene, cam, size: size, device: device, readBack: false)
            times.append(Date().timeIntervalSince(t))
        }
        times.sort()
        print("splat-perf: 300000 Gaussians, load \(String(format: "%.2f", loadS)) s, frame median \(String(format: "%.1f", times[5] * 1000)) ms (simulator on the Mac's GPU; not an iPhone figure)")
        #expect(scene.count == 300_000)
    }

    /// Renders one frame offscreen (BGRA8 sRGB + depth32) and returns its bytes (B G R A per pixel).
    static func render(_ scene: SplatScene, _ cam: StageCamera, size: CGSize, device: MTLDevice, readBack: Bool = true) async throws -> [UInt8] {
        let w = Int(size.width), h = Int(size.height)
        let cd = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: SplatScene.colorFormat, width: w, height: h, mipmapped: false)
        cd.usage = [.renderTarget, .shaderRead]
        cd.storageMode = .shared
        let dd = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: SplatScene.depthFormat, width: w, height: h, mipmapped: false)
        dd.usage = .renderTarget
        dd.storageMode = .private
        guard let color = device.makeTexture(descriptor: cd), let depth = device.makeTexture(descriptor: dd),
              let queue = device.makeCommandQueue() else { throw CocoaError(.featureUnsupported) }
        for _ in 0..<40 {   // the first frames wait for the renderer's first depth sort
            guard let cb = queue.makeCommandBuffer() else { throw CocoaError(.featureUnsupported) }
            let drew = try scene.draw(stage: cam, size: size, color: color, depth: depth, to: cb)
            cb.commit()
            await cb.completed()
            if drew { break }
            try await Task.sleep(for: .milliseconds(50))
        }
        guard readBack else { return [] }
        var out = [UInt8](repeating: 0, count: w * h * 4)
        color.getBytes(&out, bytesPerRow: w * 4, from: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0)
        return out
    }
}

/// Renders a real `.splat` from three turntable angles with the app's stage framing, as PNGs (a look, not a check).
/// Runs only when SPLAT_RENDER_IN (the file) and SPLAT_RENDER_OUT (a folder) are set, e.g.
/// `TEST_RUNNER_SPLAT_RENDER_IN=/path/a.splat TEST_RUNNER_SPLAT_RENDER_OUT=/path/out xcodebuild test ...`;
/// optional SPLAT_RENDER_FOCUS = "x,y,z,radius" (else the median and 80th-percentile spread of the Gaussians).
@MainActor
struct SplatRenderLook {
    @Test func rendersThreeAngles() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let input = env["SPLAT_RENDER_IN"], let output = env["SPLAT_RENDER_OUT"],
              let device = MTLCreateSystemDefaultDevice() else { return }
        let data = try Data(contentsOf: URL(fileURLWithPath: input))
        let file = try SplatFile.decode(data)
        var focus: (SIMD3<Float>, Float)
        if let f = env["SPLAT_RENDER_FOCUS"]?.split(separator: ",").compactMap({ Float($0) }), f.count == 4 {
            focus = (SIMD3(f[0], f[1], f[2]), f[3])
        } else {
            func median(_ k: Int) -> Float { let v = file.points.map { $0.position[k] }.sorted(); return v[v.count / 2] }
            let c = SIMD3(median(0), median(1), median(2))
            let r = file.points.map { simd_distance($0.position, c) }.sorted()
            focus = (c, r[r.count * 8 / 10])
        }
        guard let scene = await SplatScene.load(data, focus: focus) else { Issue.record("did not load"); return }
        let stage = RecreationStage()
        stage.set(.splat(scene), focus: focus)
        let size = CGSize(width: 600, height: 600)
        try FileManager.default.createDirectory(atPath: output, withIntermediateDirectories: true)
        for (i, angle) in [Float(0), 2.1, 4.2].enumerated() {
            stage.pivot.simdOrientation = simd_quatf(angle: angle, axis: SIMD3(0, 1, 0))
            let cam = StageCamera(camera: stage.cameraNode.simdWorldTransform, content: stage.content.simdWorldTransform,
                                  fieldOfView: Float(stage.cameraNode.camera!.fieldOfView), horizontal: true, near: 0.005, far: 20)
            var px = try await SplatRecreationTests.render(scene, cam, size: size, device: device)
            for j in stride(from: 0, to: px.count, by: 4) { px.swapAt(j, j + 2) }   // BGRA -> RGBA
            let ctx = CGContext(data: &px, width: 600, height: 600, bitsPerComponent: 8, bytesPerRow: 2400,
                                space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
            let image = ctx.makeImage()!
            let url = URL(fileURLWithPath: output).appending(path: "angle\(i).png") as CFURL
            let dest = CGImageDestinationCreateWithURL(url, "public.png" as CFString, 1, nil)!
            CGImageDestinationAddImage(dest, image, nil)
            #expect(CGImageDestinationFinalize(dest))
        }
        print("splat-look: \(scene.count) Gaussians, focus \(focus.0) r \(focus.1), wrote \(output)")
    }
}
