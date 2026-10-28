import CoreVideo
import Foundation
import ImageIO
import simd
import Testing
@testable import RepairOS

/// Points filling an upright box (surface samples every `step` metres).
private func solid(center: SIMD3<Float>, size: SIMD3<Float>, yaw: Float = 0, step: Float = 0.004) -> [SIMD3<Float>] {
    let r = simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0))
    var out: [SIMD3<Float>] = []
    for x in stride(from: -size.x / 2, through: size.x / 2, by: step) {
        for y in stride(from: -size.y / 2, through: size.y / 2, by: step) {
            for z in stride(from: -size.z / 2, through: size.z / 2, by: step) {
                // Surface only, like a depth sensor sees it.
                let onFace = abs(abs(x) - size.x / 2) < step || abs(abs(y) - size.y / 2) < step || abs(abs(z) - size.z / 2) < step
                if onFace { out.append(center + r.act(SIMD3(x, y, z))) }
            }
        }
    }
    return out
}

struct BoxFitterTests {
    /// A 5 x 2 x 8 cm charger-sized object on a table, with the table surface, a mug 15 cm away and a wall behind:
    /// the lock must take the object only.
    @Test func locksTheTappedSmallObjectOnly() throws {
        let planeY: Float = 0.7
        let charger = solid(center: SIMD3(0, planeY + 0.01, -0.4), size: SIMD3(0.05, 0.02, 0.08), yaw: 0.5)
        let mug = solid(center: SIMD3(0.16, planeY + 0.05, -0.4), size: SIMD3(0.08, 0.1, 0.08))
        let table = stride(from: Float(-0.3), through: 0.3, by: 0.01).flatMap { x in
            stride(from: Float(-0.7), through: -0.1, by: 0.01).map { z in SIMD3(x, planeY + 0.001, z) }
        }
        let wall = stride(from: Float(-0.3), through: 0.3, by: 0.01).flatMap { x in
            stride(from: planeY, through: planeY + 0.5, by: 0.01).map { y in SIMD3(x, y, -0.9) }
        }
        let origin = SIMD3<Float>(0, planeY + 0.35, 0)
        let tap = SIMD3<Float>(0, planeY, -0.4)
        let box = try #require(BoxFitter.fit(points: charger + mug + table + wall, tap: tap, planeY: planeY,
                                             rayOrigin: origin, rayDirection: tap - origin))
        // Tight: padded 15 %, never the 0.6 m box.
        #expect(box.size.x < 0.12 && box.size.z < 0.12)
        #expect(box.size.y >= BoxFitter.minSide && box.size.y < 0.06)
        #expect(abs(box.planeY - planeY) < 1e-4)
        #expect(charger.allSatisfy { box.contains($0, margin: 0.002) })
        #expect(!mug.contains { box.contains($0) })
    }

    @Test func fitsTheRotatedFootprint() throws {
        let pts = solid(center: SIMD3(0.3, 0.05, 0.2), size: SIMD3(0.2, 0.1, 0.06), yaw: 0.6)
        let box = try #require(BoxFitter.box(around: pts, planeY: 0))
        #expect(pts.allSatisfy { box.contains($0, margin: 0.001) })
        // A rotated 20 x 6 cm footprint fits in about 23 x 7 cm once padded, not an axis-aligned 22 x 16.
        #expect(min(box.size.x, box.size.z) < 0.09)
    }

    @Test func noClusterNearTheTapIsNoLock() {
        let far = solid(center: SIMD3(1, 0.05, 1), size: SIMD3(0.05, 0.05, 0.05))
        #expect(BoxFitter.fit(points: far, tap: .zero, planeY: 0, rayOrigin: SIMD3(0, 0.3, 0.3), rayDirection: SIMD3(0, -1, -1)) == nil)
    }
}

struct CoverageDomeTests {
    let box = ObjectBox(center: SIMD3(0, 0.05, 0), size: SIMD3(0.1, 0.1, 0.1), yaw: 0)

    @Test func cellsByElevationAndAzimuth() {
        let low = CoverageDome.cell(camera: SIMD3(0, 0.1, 0.4), box: box)!       // ~14° up
        let mid = CoverageDome.cell(camera: SIMD3(0, 0.3, 0.35), box: box)!      // ~40°
        let top = CoverageDome.cell(camera: SIMD3(0.05, 0.5, 0.05), box: box)!   // ~80°
        #expect(low / 12 == 0 && mid / 12 == 1 && top / 12 == 2)
        #expect(CoverageDome.cell(camera: SIMD3(0, -0.2, 0.3), box: box) == nil)  // under the table
        #expect(CoverageDome.cell(camera: SIMD3(0.4, 0.1, 0), box: box)! % 12 != low % 12)
        #expect(CoverageDome.id(13) == "r1s01")
    }

    @Test func cellDirectionsRoundTrip() {
        for cell in 0..<CoverageDome.cellCount {
            let cam = box.center + CoverageDome.direction(cell) * 0.4 + SIMD3(0, box.size.y / 2 - 0.05, 0) * 0
            #expect(CoverageDome.cell(camera: cam, box: box) == cell)
        }
    }

    @Test func doneAtTwentyFourCells() {
        var dome = CoverageDome()
        (0..<23).forEach { dome.cover($0) }
        #expect(!dome.isDone)
        dome.cover(23)
        #expect(dome.isDone)
        #expect(abs(dome.fraction - 24.0 / 36) < 1e-9)
    }

    @Test func distanceBandFollowsSizeWithFloor() {
        let small = ObjectBox(center: .zero, size: SIMD3(0.05, 0.02, 0.08), yaw: 0)
        #expect(CoverageDome.band(small).lowerBound == 0.15)
        let large = ObjectBox(center: .zero, size: SIMD3(0.4, 0.3, 0.4), yaw: 0)
        #expect(abs(CoverageDome.band(large).lowerBound - 0.6 * large.diagonal) < 1e-5)
        #expect(abs(CoverageDome.band(large).upperBound - 2.5 * large.diagonal) < 1e-5)
    }

    @Test func guidanceSaysOtherSideOrHigher() {
        // Camera in front (+z) looking at the box.
        var m = matrix_identity_float4x4
        m.columns.3 = SIMD4(0, 0.1, 0.4, 1)
        #expect(CoverageDome.guidance(to: 6, cameraToWorld: m, box: box) == "Other side")   // sector 6 is behind
        #expect(CoverageDome.guidance(to: 24, cameraToWorld: m, box: box) == "Go higher")   // top ring, same side
    }

    @Test func projectionFillAndFrame() {
        let p = Projection(fx: 1400, fy: 1400, cx: 960, cy: 720, width: 1920, height: 1440)
        var m = matrix_identity_float4x4
        m.columns.3 = SIMD4(0, 0.05, 0.18, 1)
        let near = p.fill(box, cameraToWorld: m)
        #expect(near.inFrame && near.fraction > 0.2)
        m.columns.3 = SIMD4(0, 0.05, 2.5, 1)
        #expect(p.fill(box, cameraToWorld: m).fraction < 0.05)
        m.columns.3 = SIMD4(0.6, 0.05, 0.25, 1)
        #expect(!p.fill(box, cameraToWorld: m).inFrame)
    }
}

struct QualityGateTests {
    func metrics(_ f: (inout QualityMetrics) -> Void = { _ in }) -> QualityMetrics {
        var m = QualityMetrics(sharpness: 100, medianSharpness: 100, meanLuma: 0.5, ambient: 800, distance: 0.3,
                               band: 0.2...0.8, fill: 0.3, inFrame: true, trackingNormal: true, angularSpeed: 0.1)
        f(&m)
        return m
    }

    @Test func acceptsAGoodFrame() {
        #expect(QualityGate.evaluate(metrics()) == QualityResult(accept: true, reason: nil, lowLight: false))
    }

    @Test func oneReasonPerReject() {
        #expect(QualityGate.evaluate(metrics { $0.inFrame = false }).reason == "Keep the object in frame")
        #expect(QualityGate.evaluate(metrics { $0.distance = 1.0 }).reason == "Too far: move closer")
        #expect(QualityGate.evaluate(metrics { $0.fill = 0.05; $0.distance = 0.5 }).reason == "Too far: move closer")
        #expect(QualityGate.evaluate(metrics { $0.distance = 0.1 }).reason == "Too close: back up")
        #expect(QualityGate.evaluate(metrics { $0.sharpness = 50 }).reason == "Blurry: hold still")
        #expect(QualityGate.evaluate(metrics { $0.angularSpeed = 1.5 }).reason == "Blurry: hold still")
    }

    @Test func smallObjectAtNearEndIsNotTooFar() {
        #expect(QualityGate.evaluate(metrics { $0.fill = 0.08; $0.distance = 0.21 }).accept)
    }

    @Test func lowLightNeverBlocksAndLowersTheBar() {
        // Normal light needs 0.7× the median; low light 0.6×, and still shoots.
        #expect(!QualityGate.evaluate(metrics { $0.sharpness = 65 }).accept)
        let r = QualityGate.evaluate(metrics { $0.ambient = 150; $0.sharpness = 65 })
        #expect(r.accept && r.lowLight && r.reason == "Too dark: add light")
        #expect(!QualityGate.evaluate(metrics { $0.ambient = 150; $0.sharpness = 50 }).accept)
    }

    @Test func weakTrackingStillShoots() {
        let r = QualityGate.evaluate(metrics { $0.trackingNormal = false })
        #expect(r.accept && r.reason == "Move slowly")
    }

    @Test func laplacianSeesEdges() {
        let flat = [UInt8](repeating: 128, count: 64 * 64)
        let checker = (0..<(64 * 64)).map { i in UInt8(((i % 64) / 4 + (i / 64) / 4) % 2 == 0 ? 0 : 255) }
        #expect(QualityGate.laplacianVariance(flat, width: 64, height: 64) == 0)
        #expect(QualityGate.laplacianVariance(checker, width: 64, height: 64) > 1000)
    }
}

struct TorchRuleTests {
    /// Feeds (ambient, luma, saturated) at 10 Hz and returns the level after each.
    func run(_ rule: inout TorchRule, _ seq: [(Double?, Double, Double)], from t0: TimeInterval = 0, hot: Bool = false) -> [Float] {
        seq.enumerated().map { i, s in rule.update(time: t0 + Double(i) * 0.1, ambient: s.0, meanLuma: s.1, saturated: s.2, hot: hot) }
    }

    @Test func turnsOnAfterOneSecondOfDim() {
        var r = TorchRule()
        let levels = run(&r, Array(repeating: (200, 0.15, 0), count: 12))
        #expect(levels[5] == 0)
        #expect(levels[11] == 0.5)
    }

    @Test func briefDipDoesNothing() {
        var r = TorchRule()
        let levels = run(&r, Array(repeating: (200, 0.15, 0), count: 5) + Array(repeating: (800, 0.5, 0), count: 20))
        #expect(levels.allSatisfy { $0 == 0 })
    }

    @Test func torchLightAloneDoesNotSwitchItOff() {
        var r = TorchRule()
        _ = run(&r, Array(repeating: (200, 0.15, 0), count: 11))
        #expect(r.level == 0.5)
        // The torch lights the scene: readings rise to "bright", but not well above the torch-lit baseline.
        let lit = run(&r, Array(repeating: (700, 0.4, 0), count: 50), from: 1.1)
        #expect(lit.allSatisfy { $0 == 0.5 })
    }

    @Test func roomLightsComingOnSwitchesItOff() {
        var r = TorchRule()
        _ = run(&r, Array(repeating: (200, 0.15, 0), count: 11))
        _ = run(&r, Array(repeating: (700, 0.4, 0), count: 10), from: 1.1)
        let after = run(&r, Array(repeating: (1500, 0.7, 0), count: 30), from: 2.1)
        #expect(after.last == 0)
        #expect(after[5] == 0.5)    // needs 2 s of it
    }

    @Test func glareStepsDownThenHoldsOff() {
        var r = TorchRule()
        _ = run(&r, Array(repeating: (200, 0.15, 0), count: 11))
        let glare = run(&r, Array(repeating: (300, 0.2, 0.1), count: 20), from: 1.1)
        #expect(glare.contains(0.3) && glare.contains(0.15))
        #expect(glare.last == 0)
        // Still dim, but glare turned it off: auto waits before trying again.
        let dim = run(&r, Array(repeating: (200, 0.15, 0), count: 30), from: 3.1)
        #expect(dim.allSatisfy { $0 == 0 })
    }

    @Test func highISOTurnsItOnInOtherwiseFineLight() {
        var r = TorchRule()
        let levels = (0..<12).map { i in r.update(time: Double(i) * 0.1, ambient: 800, meanLuma: 0.5, saturated: 0, hot: false, isoHigh: true) }
        #expect(levels.last == 0.5)
        // While ISO stays high it does not switch off even in bright readings.
        let later = (0..<40).map { i in r.update(time: 1.2 + Double(i) * 0.1, ambient: 2000, meanLuma: 0.8, saturated: 0, hot: false, isoHigh: true) }
        #expect(later.allSatisfy { $0 == 0.5 })
    }

    @Test func manualModesAndHeatCap() {
        var r = TorchRule()
        r.mode = .on
        #expect(r.update(time: 0, ambient: 1000, meanLuma: 0.8, saturated: 0, hot: false) == 0.5)
        #expect(r.update(time: 0.1, ambient: 1000, meanLuma: 0.8, saturated: 0, hot: true) == TorchRule.hotCap)
        r.mode = .off
        #expect(r.update(time: 0.2, ambient: 10, meanLuma: 0.01, saturated: 0, hot: false) == 0)
    }
}

struct WriterTests {
    @Test func plyIsGatewayReadable() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).ply")
        #expect(try PLYWriter.write([SIMD3(0.1, 0.2, -0.3), SIMD3(0, 1, 0)], to: url) == 2)
        let data = try Data(contentsOf: url)
        let end = data.range(of: Data("end_header\n".utf8))!.upperBound
        let header = String(decoding: data[..<end], as: UTF8.self)
        #expect(header.contains("format binary_little_endian 1.0") && header.contains("element vertex 2"))
        #expect(!header.contains("red"))
        #expect(data.count - end == 2 * 12)
        let x = Float(bitPattern: data[end..<(end + 4)].withUnsafeBytes { UInt32(littleEndian: $0.loadUnaligned(as: UInt32.self)) })
        #expect(abs(x - 0.1) < 1e-6)
    }

    @Test func depthPNGIsSixteenBitMillimetres() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).png")
        try ImageWriters.writeDepthPNG([0.3, 0.55, 0, .nan], width: 2, height: 2, to: url)
        let src = try #require(CGImageSourceCreateWithURL(url as CFURL, nil))
        let img = try #require(CGImageSourceCreateImageAtIndex(src, 0, nil))
        #expect(img.bitsPerComponent == 16 && img.width == 2)
        let data = try #require(img.dataProvider?.data as Data?)
        let first = data.withUnsafeBytes { $0.load(as: UInt16.self) }
        let host = img.bitmapInfo.contains(.byteOrder16Big) ? UInt16(bigEndian: first) : UInt16(littleEndian: first)
        #expect(host == 300)
    }

}

struct StillSharpnessTests {
    /// A 12 MP-sized luma plane: a crisp checker in the centre scores far higher than the same checker blurred.
    func buffer(blur: Int) throws -> CVPixelBuffer {
        var pb: CVPixelBuffer?
        CVPixelBufferCreate(nil, 1200, 900, kCVPixelFormatType_420YpCbCr8BiPlanarFullRange, nil, &pb)
        let b = try #require(pb)
        CVPixelBufferLockBaseAddress(b, [])
        let row = CVPixelBufferGetBytesPerRowOfPlane(b, 0)
        let p = CVPixelBufferGetBaseAddressOfPlane(b, 0)!.assumingMemoryBound(to: UInt8.self)
        for y in 0..<900 { for x in 0..<1200 {
            // Horizontal box blur of width `blur` over 3 px stripes = motion blur.
            var acc = 0
            for k in 0...blur { acc += ((x + k) / 3 + y / 3) % 2 == 0 ? 0 : 255 }
            p[y * row + x] = UInt8(acc / (blur + 1))
        } }
        CVPixelBufferUnlockBaseAddress(b, [])
        return b
    }

    @Test func motionBlurDropsCentreSharpness() throws {
        let crisp = ScanSession.centerSharpness(try buffer(blur: 0))
        let blurred = ScanSession.centerSharpness(try buffer(blur: 6))
        #expect(crisp > 1000)
        #expect(blurred < crisp * ScanSession.stillSharpnessRatio)
    }
}

struct FramingAndPlaneTests {
    /// The owner's case: a 51 × 46 × 57 cm box on the floor, phone held about 0.7 m away.
    let big = ObjectBox(center: SIMD3(0, 0.23, 0), size: SIMD3(0.509, 0.46, 0.572), yaw: 0)
    let p = Projection(fx: 1450, fy: 1450, cx: 960, cy: 720, width: 1920, height: 1440)

    /// Camera at `eye` looking at `target` (ARKit: camera looks down −Z, +Y up).
    func look(from eye: SIMD3<Float>, at target: SIMD3<Float>) -> simd_float4x4 {
        let back = simd_normalize(eye - target)
        let right = simd_normalize(simd_cross(SIMD3(0, 1, 0), back))
        let up = simd_cross(back, right)
        return simd_float4x4(SIMD4(right, 0), SIMD4(up, 0), SIMD4(back, 0), SIMD4(eye, 1))
    }

    @Test func bigBoxFromSeventyCentimetres() {
        let eye = SIMD3<Float>(0, 0.5, 0.286 + 0.7)
        let m = look(from: eye, at: big.center)
        // Depth along the optical axis to the front face (z = +0.286).
        let dir = simd_normalize(big.center - eye)
        let depth = (big.center.z + big.size.z / 2 - eye.z) / dir.z
        let r = FramingGate.evaluate(box: big, cameraToWorld: m, projection: p, centerDepth: depth)
        #expect(r.orbit && r.close && r.centerHitsBox == true)
    }

    @Test func bigBoxCloseUpOffCentre() {
        // 30 cm from the front face, aimed at a spot near the top-left corner: the box centre is off screen.
        let target = SIMD3<Float>(-0.2, 0.42, 0.286)
        let eye = target + SIMD3(0, 0, 0.3)
        let m = look(from: eye, at: target)
        let r = FramingGate.evaluate(box: big, cameraToWorld: m, projection: p, centerDepth: 0.3)
        #expect(r.orbit && r.close)
        #expect(r.centralShare >= FramingGate.minCentralShare)
    }

    @Test func centreRayMissingTheBoxIsNotFramed() {
        let eye = SIMD3<Float>(0, 0.5, 1.0)
        let m = look(from: eye, at: SIMD3(1.5, 0.3, 1.0))   // looking sideways, at a wall 1.5 m away
        let r = FramingGate.evaluate(box: big, cameraToWorld: m, projection: p, centerDepth: 1.5)
        #expect(!r.orbit && !r.close && r.centerHitsBox == false)
    }

    @Test func estimatedPlaneTooLowDoesNotSwallowTheFloor() throws {
        // Dense floor at y = 0; the estimated plane says −2 cm. A 20 × 15 × 20 cm object stands on it.
        let floor = stride(from: Float(-0.4), through: 0.4, by: 0.008).flatMap { x in
            stride(from: Float(-0.4), through: 0.4, by: 0.008).map { z in SIMD3(x, Float.random(in: -0.002...0.002), z) }
        }
        var object: [SIMD3<Float>] = []
        for x in stride(from: Float(-0.1), through: 0.1, by: 0.006) {
            for z in stride(from: Float(-0.075), through: 0.075, by: 0.006) { object.append(SIMD3(x, 0.15, z)) }
            for y in stride(from: Float(0.01), through: 0.15, by: 0.006) { object.append(SIMD3(x, y, 0.075)) }
        }
        let tap = SIMD3<Float>(0, -0.02, 0), origin = SIMD3<Float>(0, 0.5, 0.5)
        let refined = try #require(BoxFitter.fit(points: floor + object, tap: tap, planeY: -0.02, rayOrigin: origin,
                                                 rayDirection: SIMD3(0, 0.08, 0) - origin, refinePlane: true))
        #expect(refined.size.x < 0.3 && refined.size.z < 0.3 && refined.size.y < 0.22)
        #expect(abs(refined.planeY) < 0.006)
        let naive = BoxFitter.fit(points: floor + object, tap: tap, planeY: -0.02, rayOrigin: origin,
                                  rayDirection: SIMD3(0, 0.08, 0) - origin, refinePlane: false)
        #expect((naive?.size.x ?? 1) > 0.5)   // without the refinement the floor joins the cluster
    }
}

struct ContinuationTests {
    /// A continued scan reloads the first visit's cloud from its PLY; what PLYWriter wrote must come back exactly.
    @Test func cloudRoundTrips() throws {
        let url = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString).ply")
        let pts: [SIMD3<Float>] = [SIMD3(0.1, 0.2, -0.3), SIMD3(-1, 0.5, 2)]
        try PLYWriter.write(pts, colors: [SIMD3(1, 2, 3), SIMD3(250, 128, 0)], to: url)
        let back = try #require(ScanCloudReader.read(url))
        #expect(back.map(\.0) == pts)
        #expect(back.map(\.1) == [SIMD3(1, 2, 3), SIMD3(250, 128, 0)])
    }
}

struct FlatObjectTests {
    /// Gaussian-ish noise from the sum of uniforms (deterministic generator, so the test is stable).
    struct Noise {
        var state: UInt64 = 42
        mutating func next() -> Float {
            state = state &* 6364136223846793005 &+ 1442695040888963407
            return Float(state >> 40) / Float(1 << 24)
        }
        mutating func gauss(_ sigma: Float) -> Float { (next() + next() + next() + next() - 2) * sigma * 1.73 }
    }

    /// A 70 × 55 × 2 mm slab (one colour) with a few 1 cm blocks, on a plane (another colour) with ~3 mm of height
    /// noise: the slab hides in the noise, so the box must come from colour, and must cover the whole slab.
    func scene(blocks: Bool) -> (pts: [SIMD3<Float>], cols: [SIMD3<UInt8>], slab: [SIMD3<Float>]) {
        var n = Noise()
        var pts: [SIMD3<Float>] = [], cols: [SIMD3<UInt8>] = [], slab: [SIMD3<Float>] = []
        let step: Float = 0.004
        for x in stride(from: Float(-0.35), through: 0.35, by: step) {
            for z in stride(from: Float(-0.35), through: 0.35, by: step) {
                let onSlab = abs(x) <= 0.035 && abs(z) <= 0.0275
                let y = (onSlab ? 0.002 : 0) + n.gauss(0.0015)
                pts.append(SIMD3(x, y, z))
                cols.append(onSlab ? SIMD3(20, 110, 70) : SIMD3(150, 150, 150))
                if onSlab { slab.append(SIMD3(x, y, z)) }
            }
        }
        if blocks {
            for (bx, bz) in [(Float(-0.02), Float(0.01)), (0.015, -0.015)] {
                for x in stride(from: bx - 0.004, through: bx + 0.004, by: 0.002) {
                    for z in stride(from: bz - 0.004, through: bz + 0.004, by: 0.002) {
                        for y in stride(from: Float(0.004), through: 0.012, by: 0.002) {
                            pts.append(SIMD3(x, y, z))
                            cols.append(SIMD3(30, 30, 30))
                        }
                    }
                }
            }
        }
        return (pts, cols, slab)
    }

    @Test func slabWithBlocksIsCoveredWhole() throws {
        let s = scene(blocks: true)
        let origin = SIMD3<Float>(0, 0.3, 0.2)
        let box = try #require(BoxFitter.fit(points: s.pts, colors: s.cols, tap: .zero, planeY: 0, rayOrigin: origin,
                                             rayDirection: -origin, refinePlane: true))
        let outside = s.slab.filter { !box.contains(SIMD3($0.x, box.center.y, $0.z), margin: 0.001) }
        #expect(outside.isEmpty, "\(outside.count) of \(s.slab.count) slab points outside the box")
        #expect(box.size.x < 0.15 && box.size.z < 0.15)
    }

    @Test func bareSlabStillLocks() throws {
        let s = scene(blocks: false)
        let origin = SIMD3<Float>(0, 0.3, 0.2)
        let box = try #require(BoxFitter.fit(points: s.pts, colors: s.cols, tap: .zero, planeY: 0, rayOrigin: origin,
                                             rayDirection: -origin, refinePlane: true))
        #expect(s.slab.allSatisfy { box.contains(SIMD3($0.x, box.center.y, $0.z), margin: 0.001) })
        #expect(box.size.x >= BoxFitter.minFootprint && box.size.z >= BoxFitter.minFootprint)
    }

    @Test func viewRayDistanceToTheBox() {
        let box = ObjectBox(center: SIMD3(0, 0.01, 0), size: SIMD3(0.08, 0.02, 0.08), yaw: 0.4)
        var m = matrix_identity_float4x4                 // looking down −Z
        m.columns.3 = SIMD4(0, 0.01, 0.2, 1)
        #expect(abs((ScanSession.rayBoxDistance(cameraToWorld: m, box: box) ?? 0) - 0.2 + 0.04 / cos(0.4)) < 0.01)
        m.columns.3 = SIMD4(0.5, 0.01, 0.2, 1)
        #expect(ScanSession.rayBoxDistance(cameraToWorld: m, box: box) == nil)
    }
}
