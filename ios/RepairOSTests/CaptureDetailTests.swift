import CoreVideo
import Foundation
import Testing
import simd
@testable import RepairOS

/// Photos close and sharp enough for a real inspection: framing by object size, resolution on the object,
/// close-ups of every face of a small object, full torch up close, in-box sharpness.
struct DetailTargetTests {
    let small = ObjectBox(center: SIMD3(0, 0.02, 0), size: SIMD3(0.17, 0.04, 0.08), yaw: 0)
    let large = ObjectBox(center: SIMD3(0, 0.2, 0), size: SIMD3(0.5, 0.4, 0.4), yaw: 0)

    @Test func smallObjectsAreShotFromCloseAndLargeOnesFromTheirBand() {
        #expect(DetailTarget.isSmall(small))
        #expect(!DetailTarget.isSmall(large))
        let orbit = DetailTarget.orbitBand(box: small, minFocus: 0.12)
        #expect(orbit.lowerBound == 0.15 && orbit.upperBound == 0.25)
        // A camera that only focuses from 20 cm moves the near end out, never inside its focus.
        #expect(DetailTarget.orbitBand(box: small, minFocus: 0.2).lowerBound >= 0.21 - 1e-6)
        #expect(DetailTarget.orbitBand(box: large, minFocus: 0.12).upperBound == CoverageDome.band(large).upperBound)
    }

    @Test func closeUpsGoToTheNearestSharpDistance() {
        let band = DetailTarget.closeBand(box: small, minFocus: 0.12)
        #expect(abs(band.lowerBound - 0.126) < 1e-4)
        #expect(abs(band.upperBound - (0.126 + DetailTarget.closeSpan)) < 1e-4)
        #expect(DetailTarget.closeBand(box: small, minFocus: 0.2).lowerBound >= 0.21 - 1e-6)
        #expect(DetailTarget.closeBand(box: large, minFocus: 0.12).lowerBound == 0.15)
    }

    @Test func resolutionTargets() {
        // A 12 MP still (fx ≈ 2915 px) at 25 cm: 11.7 px/mm.
        #expect(abs(DetailTarget.pxPerMm(fx: 2915, distance: 0.25) - 11.66) < 0.01)
        // Small object: 8 px/mm; a large one can only reach what filling half the frame gives.
        #expect(DetailTarget.orbitTarget(box: small, shortSidePx: 3024) == 8)
        #expect(DetailTarget.orbitTarget(box: large, shortSidePx: 3024) < 8)
        // Close-ups: 15 px/mm when the camera focuses close enough, else 90 % of its best.
        #expect(DetailTarget.closeTarget(fx: 2915, minFocus: 0.12) == 15)
        let far = DetailTarget.closeTarget(fx: 2915, minFocus: 0.2)
        #expect(far < 15 && abs(far - 0.9 * 2915 / 210) < 0.01)
    }

    @Test func approachCueSaysHowClose() {
        let band: ClosedRange<Float> = 0.15...0.25
        // At 40 cm with the object across 30 % of the short side: aim inside the band.
        let d = DetailTarget.approachDistance(current: 0.4, fill: 0.3, band: band)
        #expect(d >= band.lowerBound && d <= band.upperBound)
        #expect(DetailTarget.approachCue(current: 0.4, fill: 0.3, band: band).hasPrefix("Move closer: about "))
        // Never further than where the person already is.
        #expect(DetailTarget.approachDistance(current: 0.16, fill: 0.9, band: band) <= 0.16)
    }

    @Test func qualityGateRefusesASmallFarObject() {
        let m = QualityMetrics(sharpness: 100, medianSharpness: 100, meanLuma: 0.5, ambient: 1000, distance: 0.4,
                               band: DetailTarget.orbitBand(box: small, minFocus: 0.12), fill: 0.3, inFrame: true,
                               trackingNormal: true, angularSpeed: 0, minFill: DetailTarget.orbitFill)
        #expect(QualityGate.evaluate(m).reason == "Too far: move closer")
        var near = m
        near.distance = 0.2
        near.fill = 0.6
        #expect(QualityGate.evaluate(near).accept)
    }
}

struct ProjectionExtentTests {
    /// A camera looking straight down from `height` above the origin.
    func lookingDown(_ height: Float) -> simd_float4x4 {
        // Camera −Z points down (world −Y): rotate +90° about X.
        var m = simd_float4x4(simd_quatf(angle: -.pi / 2, axis: SIMD3(1, 0, 0)))
        m.columns.3 = SIMD4(0, height, 0, 1)
        return m
    }

    @Test func extentGrowsAsTheCameraComesCloser() {
        let p = Projection(fx: 1400, fy: 1400, cx: 960, cy: 720, width: 1920, height: 1440)
        let box = ObjectBox(center: SIMD3(0, 0.02, 0), size: SIMD3(0.15, 0.04, 0.05), yaw: 0)
        let far = p.extent(box, cameraToWorld: lookingDown(0.42))
        let near = p.extent(box, cameraToWorld: lookingDown(0.22))
        #expect(far < 0.5)
        #expect(near > far * 1.8)
        // So close part of the box is behind the camera: it fills the view.
        #expect(p.extent(box, cameraToWorld: lookingDown(0.01)) == 1)
    }
}

struct CloseFaceTests {
    let box = ObjectBox(center: SIMD3(0, 0.02, 0), size: SIMD3(0.17, 0.04, 0.08), yaw: 0.3)

    func camera(at p: SIMD3<Float>, lookingAt target: SIMD3<Float>) -> simd_float4x4 {
        let fwd = simd_normalize(target - p)
        let right = simd_normalize(simd_cross(fwd, SIMD3(0, 1, 0) + SIMD3(0.001, 0, 0)))
        let up = simd_cross(right, fwd)
        return simd_float4x4(columns: (SIMD4(right, 0), SIMD4(up, 0), SIMD4(-fwd, 0), SIMD4(p, 1)))
    }

    @Test func viewFaceIsTheFaceTheCentreRayEnters() {
        for face in 0..<CoverageDome.faceCount {
            let c = CoverageDome.faceCenter(face, box: box)
            let r = simd_quatf(angle: box.yaw, axis: SIMD3(0, 1, 0))
            let out = r.act(CoverageDome.faceNormal(face))
            let cam = camera(at: c + out * 0.14 + (face == 0 ? SIMD3(0.01, 0, 0) : SIMD3(0, 0.01, 0)), lookingAt: c)
            #expect(CoverageDome.viewFace(cameraToWorld: cam, box: box) == face)
        }
    }

    @Test func notDoneUntilEveryRequiredFaceHasItsCloseUp() {
        var dome = CoverageDome()
        dome.requiredFaces = CoverageDome.faceCount
        (0..<CoverageDome.doneCells).forEach { dome.cover($0) }
        #expect(!dome.isDone)
        // The marker goes to the missing face that is turned most toward the camera.
        let above = box.center + SIMD3(0, 0.3, 0)
        #expect(dome.nearestMissingFace(camera: above, box: box) == 0)
        (0..<CoverageDome.faceCount).forEach { dome.coverFace($0) }
        #expect(dome.isDone)
        #expect(dome.nearestMissingFace(camera: above, box: box) == nil)
    }

    @Test func largeObjectsNeedNoFaces() {
        var dome = CoverageDome()
        (0..<CoverageDome.doneCells).forEach { dome.cover($0) }
        #expect(dome.isDone)
    }
}

struct TorchNearTests {
    @Test func fullTorchUpCloseAndBackToTheFarLevel() {
        var r = TorchRule()
        var level: Float = 0
        for i in 0..<12 { level = r.update(time: Double(i) * 0.1, ambient: 200, meanLuma: 0.15, saturated: 0, hot: false, near: true) }
        #expect(level == TorchRule.nearLevel)
        for i in 0..<5 { level = r.update(time: 1.2 + Double(i) * 0.1, ambient: 200, meanLuma: 0.15, saturated: 0, hot: false, near: false) }
        #expect(level == TorchRule.levels[0])
        level = r.update(time: 1.8, ambient: 200, meanLuma: 0.15, saturated: 0, hot: false, near: true)
        #expect(level == TorchRule.nearLevel)
    }

    @Test func noisyISOUpCloseTurnsItOnInOtherwiseFineLight() {
        var r = TorchRule()
        var level: Float = 0
        for i in 0..<12 { level = r.update(time: Double(i) * 0.1, ambient: 900, meanLuma: 0.5, saturated: 0, hot: false, near: true, noisy: true) }
        #expect(level == TorchRule.nearLevel)
        // Far away a little noise alone does not.
        var far = TorchRule()
        for i in 0..<12 { level = far.update(time: Double(i) * 0.1, ambient: 900, meanLuma: 0.5, saturated: 0, hot: false, near: false, noisy: true) }
        #expect(level == 0)
    }

    @Test func glareOnTheObjectStepsDownOneNotchAndRecovers() {
        var r = TorchRule()
        for i in 0..<12 { _ = r.update(time: Double(i) * 0.1, ambient: 200, meanLuma: 0.15, saturated: 0, hot: false, near: true) }
        let stepped = r.update(time: 1.2, ambient: 200, meanLuma: 0.15, saturated: 0.1, hot: false, near: true)
        #expect(stepped == 0.7)
        // Quiet for the hold time: back up a notch.
        var level = stepped
        for i in 0..<120 { level = r.update(time: 1.3 + Double(i) * 0.1, ambient: 200, meanLuma: 0.15, saturated: 0, hot: false, near: true) }
        #expect(level == TorchRule.nearLevel)
    }

    @Test func heatCapsTheNearLevel() {
        var r = TorchRule()
        var level: Float = 0
        for i in 0..<12 { level = r.update(time: Double(i) * 0.1, ambient: 200, meanLuma: 0.15, saturated: 0, hot: true, near: true) }
        #expect(level == TorchRule.hotCap)
    }
}

struct BoxSharpnessTests {
    func buffer(_ w: Int, _ h: Int, _ px: (Int, Int) -> UInt8) -> CVPixelBuffer {
        var pb: CVPixelBuffer?
        CVPixelBufferCreate(nil, w, h, kCVPixelFormatType_420YpCbCr8BiPlanarFullRange, nil, &pb)
        let b = pb!
        CVPixelBufferLockBaseAddress(b, [])
        let base = CVPixelBufferGetBaseAddressOfPlane(b, 0)!.assumingMemoryBound(to: UInt8.self)
        let row = CVPixelBufferGetBytesPerRowOfPlane(b, 0)
        for y in 0..<h { for x in 0..<w { base[y * row + x] = px(x, y) } }
        CVPixelBufferUnlockBaseAddress(b, [])
        return b
    }

    @Test func measuresInsideTheBoxOnly() {
        // Sharp texture on the left half, flat on the right.
        let b = buffer(256, 128) { x, y in x < 128 ? UInt8(((x / 4 + y / 4) % 2) * 255) : 128 }
        let left = ScanSession.boxSharpness(b, rect: (0, 0, 128, 128))
        let right = ScanSession.boxSharpness(b, rect: (128, 0, 256, 128))
        #expect(left > 1000)
        #expect(right == 0)
        #expect(ScanSession.boxSharpness(b, rect: nil) > right)
    }
}

struct DistanceCueVoiceTests {
    @Test func distanceCueIsSaidByItsFirstPart() {
        #expect(VoiceCues.line(for: "Move closer: about 22 cm away") == "Move closer.")
        #expect(VoiceCues.line(for: "Move closer here") == "Move closer here.")
        #expect(VoiceCues.line(for: "Blurry: hold still") == nil)
    }
}
