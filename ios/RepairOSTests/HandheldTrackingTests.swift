import Foundation
import simd
import Testing
@testable import RepairOS

/// The hand-held tracker on synthetic scans with ground truth (HandheldSynthetic.swift): a textured box and a plain
/// cylinder with one label-like patch, turned ~45°/s in front of a nearly still phone, LiDAR depth with 3 mm noise,
/// the hand in the person mask. The Mac harness (tools/handheld/run.sh) runs the same scenes longer and optimised.
struct HandheldMathTests {
    @Test func hornRecoversARigidTransform() throws {
        var rng = SplitMix64(seed: 7)
        let r = simd_float3x3(simd_quatf(angle: 0.7, axis: simd_normalize(SIMD3(0.3, 1, -0.2))))
        let t = Rigid.make(r, SIMD3(0.1, -0.05, 0.3))
        let src = (0..<30).map { _ in SIMD3<Float>(rng.gaussian(), rng.gaussian(), rng.gaussian()) * 0.05 }
        let dst = src.map { Rigid.apply(t, $0) }
        let fit = try #require(DenseSolve.absoluteOrientation(src, dst))
        #expect(Rigid.distance(fit, t, probe: src) < 1e-4)
    }

    @Test func constantVelocityContinuesATurnAboutThePivot() {
        let c = SIMD3<Float>(0.2, 0.8, -0.4)
        func turn(_ a: Float) -> simd_float4x4 {
            let r = simd_float3x3(simd_quatf(angle: a, axis: SIMD3(0, 1, 0)))
            return Rigid.make(r, c - r * c)
        }
        let p = Rigid.extrapolate(turn(0.1), at: 0, turn(0.2), at: 0.1, to: 0.2, pivot: c)
        #expect(Rigid.distance(p, turn(0.3), probe: [c + SIMD3(0.05, 0, 0)]) < 1e-4)
    }

    @Test func fastFindsCornersOfAPattern() {
        // Bright squares apart on a dark ground: FAST fires on their (L-shaped) corners; a copy shifted by 3 px
        // gives the same descriptors at the shifted corners.
        func image(shift: Int) -> GrayImage {
            var px = [UInt8](repeating: 40, count: 160 * 120)
            for y in 0..<120 { for x in 0..<160 where (x - shift) % 28 < 12 && y % 28 < 12 && x >= shift { px[y * 160 + x] = 210 } }
            return GrayImage(width: 160, height: 120, pixels: px)
        }
        let a = FeatureDetector.detect(image(shift: 0), maxCount: 100), b = FeatureDetector.detect(image(shift: 3), maxCount: 100)
        #expect(a.count >= 10)
        let same = a.filter { k in b.contains { abs($0.x - k.x - 3) < 1 && abs($0.y - k.y) < 1 && $0.descriptor.distance(k.descriptor) < 30 } }
        #expect(same.count >= a.count / 2)
    }

    @Test func cubeRotationsAreTheTwentyFourTurns() {
        #expect(ObjectTracker.cubeRotations.count == 24)
        #expect(ObjectTracker.cubeRotations.allSatisfy { abs($0.determinant - 1) < 1e-5 })
    }
}

/// Short sequences (the test build is not optimised); the summaries go to the test log.
@Suite(.serialized)
struct HandheldTrackingTests {
    /// Fewer source points and features keep the unoptimised test build quick; the harness runs the full config.
    static var testConfig: TrackerConfig {
        var c = TrackerConfig.standard
        c.maxSourcePoints = 700
        c.maxFeatures = 200
        return c
    }

    @Test func tracksATexturedBoxTurnedInTheHand() {
        let scene = SyntheticScene(.texturedBox)
        let run = SyntheticRun.run(scene, frames: 40, config: Self.testConfig)
        print(String(format: "handheld box: %d frames, turned %.0f°, error mean %.2f mm / %.2f°, max %.2f mm / %.2f°",
                     run.rows.count, run.turnedDeg, run.meanMM, run.meanDeg, run.maxMM, run.maxDeg))
        #expect(run.rows.count == 40)          // never lost
        #expect(run.turnedDeg > 50)
        #expect(run.meanMM < 4)
        #expect(run.maxMM < 9)
        #expect(run.meanDeg < 3)
    }

    @Test func holdsASymmetricCylinderWithALabel() {
        let scene = SyntheticScene(.cylinderWithPatch)
        let run = SyntheticRun.run(scene, frames: 40, config: Self.testConfig)
        let sure = run.rows.filter { $0.quality >= 0.7 }
        let sureMM = sure.isEmpty ? 0 : sure.map(\.mm).reduce(0, +) / Float(sure.count)
        print(String(format: "handheld cylinder: %d frames, error mean %.2f mm / %.2f°, max %.2f mm; quality ≥ 0.7: %d frames, mean %.2f mm",
                     run.rows.count, run.meanMM, run.meanDeg, run.maxMM, sure.count, sureMM))
        #expect(run.rows.count == 40)
        // Its spin is only seen through the label: honest about it (some frames below 0.7), close when it is sure.
        #expect(sure.count >= 10)
        #expect(sureMM < 12)
        #expect(run.meanMM < 20)
    }

    @Test func lostWhileHiddenThenRecovered() {
        let scene = SyntheticScene(.texturedBox)
        var states: [TrackResult.State] = []
        let run = SyntheticRun.run(scene, frames: 52, occlude: 30..<36, config: Self.testConfig) { _, r in states.append(r.state) }
        let lostAt = states.firstIndex(of: .lost)
        let recoveredAt = states.firstIndex(of: .recovered)
        print("handheld lost/recovered: lost at \(lostAt ?? -1), recovered at \(recoveredAt ?? -1); after: max \(run.rows.filter { $0.t > 36.0 / 15 }.map(\.mm).max() ?? -1) mm")
        #expect(lostAt.map { (30...32).contains($0) } ?? false)
        #expect(recoveredAt.map { (36...40).contains($0) } ?? false)
        let after = run.rows.filter { $0.t > 40.0 / 15 }
        #expect(!after.isEmpty)
        #expect((after.map(\.mm).max() ?? 99) < 10)
    }
}
