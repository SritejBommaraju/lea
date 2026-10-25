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

/// "You have enough": one progress number from what the backend needs, and the on-phone selection.
struct ScanProgressTests {
    /// Covers `n` side sectors (alternating the two lower rings) and `top` top sectors.
    func dome(side: Int, top: Int, faces: Int = 0, required: Int = 0) -> CoverageDome {
        var d = CoverageDome()
        d.requiredFaces = required
        for s in 0..<side { d.cover((s % 2) * CoverageDome.sectors + s) }
        for s in 0..<top { d.cover(2 * CoverageDome.sectors + s) }
        for f in 0..<faces { d.coverFace(f) }
        return d
    }

    @Test func largeObjectIsEnoughWithViewsAndMesh() {
        #expect(ScanProgress.main(dome: dome(side: 0, top: 0), keyframeSectors: 0).value == 0)
        let half = ScanProgress.main(dome: dome(side: 4, top: 1), keyframeSectors: 4)
        #expect(half.value > 0.4 && half.value < 0.6 && !half.enough)
        let full = ScanProgress.main(dome: dome(side: 8, top: 2), keyframeSectors: 9)
        #expect(full.value >= 1 - 1e-9 && full.enough && full.percent == 100)
        // Good enough at 85 %: one top view and a sector short still stops.
        let most = ScanProgress.main(dome: dome(side: 7, top: 2), keyframeSectors: 9)
        #expect(most.value >= ScanProgress.goodEnough && most.enough)
        // Repeat views of the same sector add nothing.
        var same = CoverageDome()
        same.cover(0); same.cover(CoverageDome.sectors)
        #expect(same.sideSectors == 1)
    }

    @Test func smallObjectNeedsEveryFace() {
        let noFaces = ScanProgress.main(dome: dome(side: 8, top: 2, faces: 4, required: 5), keyframeSectors: 9)
        #expect(noFaces.value >= ScanProgress.goodEnough && !noFaces.enough)
        let all = ScanProgress.main(dome: dome(side: 8, top: 2, faces: 5, required: 5), keyframeSectors: 9)
        #expect(all.enough)
    }

    @Test func bottomNeedsViewsMeshAndLineUpOrTheWait() {
        let d = dome(side: 6, top: 0)
        #expect(!ScanProgress.flip(dome: d, keyframeSectors: 6, linedUp: false, elapsed: 10).enough)
        #expect(ScanProgress.flip(dome: d, keyframeSectors: 6, linedUp: true, elapsed: 10).enough)
        #expect(ScanProgress.flip(dome: d, keyframeSectors: 6, linedUp: false, elapsed: ScanProgress.flipAlignWait).enough)
        #expect(!ScanProgress.flip(dome: dome(side: 3, top: 0), keyframeSectors: 6, linedUp: true, elapsed: 60).enough)
        // At least 15 bottom keyframes (a 6-keyframe bottom only just registered on the server).
        #expect(!ScanProgress.flip(dome: d, keyframeSectors: 6, linedUp: true, elapsed: 60, keyframes: 6).enough)
        #expect(ScanProgress.flip(dome: d, keyframeSectors: 6, linedUp: true, elapsed: 60, keyframes: 15).enough)
    }

    @Test func nextNeededIsASideSectorThenTheTop() {
        let box = ObjectBox(center: SIMD3(0, 0.05, 0), size: SIMD3(0.1, 0.1, 0.1), yaw: 0)
        let cam = box.center + CoverageDome.direction(1) * 0.3
        let next = dome(side: 3, top: 0).nearestNeeded(camera: cam, box: box)!
        #expect(next / CoverageDome.sectors < 2 && next % CoverageDome.sectors >= 3)
        let top = dome(side: 8, top: 0).nearestNeeded(camera: cam, box: box)!
        #expect(top / CoverageDome.sectors == 2)
        #expect(dome(side: 8, top: 2).nearestNeeded(camera: cam, box: box) == nil)
    }

    @Test func progressCueCarriesTheNumber() {
        #expect(ScanSession.progressCue(74, "Keep going") == "74% — keep going")
        #expect(ScanSession.progressCue(40, "Move right") == "40% — move right")
    }
}

struct CaptureSelectionTests {
    func pose(_ p: SIMD3<Float>, yaw: Float = 0) -> simd_float4x4 {
        var m = simd_float4x4(simd_quatf(angle: yaw, axis: SIMD3(0, 1, 0)))
        m.columns.3 = SIMD4(p, 1)
        return m
    }

    @Test func stillOnlyForANewCellOrAClearlyBetterView() {
        let best: [Int: CaptureSelection.Best] = [3: .init(id: "a", pxPerMm: 10, sharpness: 100)]
        #expect(CaptureSelection.worthShooting(cell: 4, predictedPx: 9, best: best))
        #expect(!CaptureSelection.worthShooting(cell: 3, predictedPx: 11, best: best))
        #expect(CaptureSelection.worthShooting(cell: 3, predictedPx: 13, best: best))
        #expect(CaptureSelection.better(pxPerMm: 13, sharpness: 80, than: best[3]!))
        #expect(CaptureSelection.better(pxPerMm: 10, sharpness: 140, than: best[3]!))
        #expect(!CaptureSelection.better(pxPerMm: 10.5, sharpness: 110, than: best[3]!))
    }

    @Test func keyframeOnlyFromANewViewpointAndCapped() {
        let kept = [pose(.zero)]
        #expect(CaptureSelection.keyframe(pose: pose(SIMD3(0.02, 0, 0)), kept: kept, cell: 1, cellsWithKeyframes: []) == .near)
        #expect(CaptureSelection.keyframe(pose: pose(SIMD3(0.08, 0, 0)), kept: kept, cell: 1, cellsWithKeyframes: []) == .keep)
        // Same spot, turned well away: a new view.
        #expect(CaptureSelection.keyframe(pose: pose(.zero, yaw: 0.3), kept: kept, cell: 1, cellsWithKeyframes: []) == .keep)
        let many = (0..<CaptureSelection.keyframeSoftCap).map { pose(SIMD3(Float($0) * 0.1, 0, 0)) }
        #expect(CaptureSelection.keyframe(pose: pose(SIMD3(0, 1, 0)), kept: many, cell: 2, cellsWithKeyframes: [2]) == .softCap)
        #expect(CaptureSelection.keyframe(pose: pose(SIMD3(0, 1, 0)), kept: many, cell: 5, cellsWithKeyframes: [2]) == .keep)
        let full = (0..<CaptureSelection.maxKeyframes).map { pose(SIMD3(Float($0) * 0.1, 0, 0)) }
        #expect(CaptureSelection.keyframe(pose: pose(SIMD3(0, 2, 0)), kept: full, cell: 9, cellsWithKeyframes: []) == .cap)
    }
}
