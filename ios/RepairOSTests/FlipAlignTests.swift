import Foundation
import simd
import Testing
@testable import RepairOS

/// "Scan the bottom": registering a mini-scan of the flipped object to the main scan (FlipAligner) on synthetic
/// clouds with the true transform (HandheldSynthetic.swift, SyntheticFlip). Accepting a wrong pose is the failure
/// that matters; refusing (the backend then registers it) is allowed.
struct FlipAlignTests {
    static func run(_ shape: SyntheticFlip.Shape, trials: Int) -> (ok: Int, wrong: Int, refused: Int, maxMM: Float) {
        var ok = 0, wrong = 0, refused = 0, maxMM: Float = 0
        for k in 0..<trials {
            let c = SyntheticFlip.make(shape, seed: UInt64(300 + k), flipAboutZ: k % 2 == 1)
            guard let r = FlipAligner.align(target: c.target, source: c.source), r.accepted else { refused += 1; continue }
            let e = SyntheticFlip.error(r.toSegment0, c) * 1000
            if e < 10 { ok += 1; maxMM = max(maxMM, e) } else { wrong += 1 }
        }
        print("flip \(shape): \(ok)/\(trials) aligned (max \(maxMM) mm), \(wrong) wrong, \(refused) refused")
        return (ok, wrong, refused, maxMM)
    }

    @Test func linesUpAFlippedTexturedBox() {
        let r = Self.run(.texturedBox, trials: 6)
        #expect(r.wrong == 0)
        #expect(r.ok >= 5)
        #expect(r.maxMM < 6)
    }

    @Test func linesUpAFlippedFlatBoardByItsSticker() {
        let r = Self.run(.flatBoard, trials: 8)
        #expect(r.wrong == 0)
        #expect(r.ok >= 6)
        #expect(r.maxMM < 6)
    }

    @Test func refusesAPlainSymmetricBoxRatherThanGuess() {
        let r = Self.run(.plainBox, trials: 4)
        #expect(r.wrong == 0)
    }

    @Test func footprintCoversTheUnderside() {
        let c = SyntheticFlip.make(.texturedBox, seed: 1)
        let f = FlipAligner.footprint(c.target)
        // 9 × 6 cm footprint at 4 mm: ~340 cells, all on the plane.
        #expect(f.count > 250)
        #expect(f.allSatisfy { abs($0.y - c.target.box.planeY) < 1e-5 })
    }
}

/// The bottom scan's carry-on cue: the sides (what both scans share) until it lines up, after a few seconds.
struct FlipGuidanceTests {
    @Test func asksForTheSidesUntilLinedUp() {
        typealias F = FlipSegmentController
        #expect(F.steadyCue(replacing: "Keep walking around it", aligned: false, elapsed: 2) == "Keep going around the bottom")
        #expect(F.steadyCue(replacing: "Keep walking around it", aligned: false, elapsed: 9) == F.sidesCue)
        #expect(F.steadyCue(replacing: "Keep walking around it", aligned: true, elapsed: 30) == "Keep going around the bottom")
        #expect(F.steadyCue(replacing: "Move closer", aligned: false, elapsed: 30) == nil)
        #expect(F.prompt.contains("onto a side") && F.prompt.contains("firm surface") && F.prompt.contains("sides"))
    }

    @Test func escalatesUntilLinedUpAndAsksAFlatObjectForItsEdge() {
        typealias F = FlipSegmentController
        let walk = "Keep walking around it"
        #expect(F.steadyCue(replacing: walk, aligned: false, elapsed: 25) == F.lowerCue)          // more firmly
        #expect(F.steadyCue(replacing: walk, aligned: true, elapsed: 25) == "Keep going around the bottom")
        #expect(F.steadyCue(replacing: walk, aligned: false, elapsed: 5, flat: true) == F.flatCue)
        #expect(F.steadyCue(replacing: walk, aligned: true, elapsed: 30, flat: true) == "Keep going around the bottom")
        // Locked boxes of the real flips: a board's 4 cm box is flat, a controller's or a mouse's 7-9 cm is not.
        #expect(F.isFlat(TrackBox(center: .zero, half: SIMD3(0.19, 0.02, 0.2), yaw: 0)))
        #expect(!F.isFlat(TrackBox(center: .zero, half: SIMD3(0.21, 0.034, 0.2), yaw: 0)))
        for cue in [F.sidesCue, F.lowerCue, F.flatCue] { #expect(cue.count <= 60 && ScanSession.steadyCues.contains(cue)) }
    }
}

/// The object alone before FlipAligner (FlipIsolation): the locked box 3x the object with its floor 2 cm under a
/// table that leans 2 deg, as on the real flips. The table is found and cut, the clutter beside the object dropped,
/// and the box drawn tight with its floor on the table.
struct FlipIsolationTests {
    static func scene() -> (FlipCloud, SIMD3<Float>) {
        var rng = SplitMix64(seed: 11)
        var pts: [SIMD3<Float>] = []
        let tilt: Float = 2 * .pi / 180, tableY: Float = 0.75
        func table(_ x: Float, _ z: Float) -> Float { tableY + tan(tilt) * x }
        // Table everywhere in the box, with millimetre noise.
        var x: Float = -0.21
        while x <= 0.21 {
            var z: Float = -0.2
            while z <= 0.2 { pts.append(SIMD3(x, table(x, z) + 0.0015 * rng.gaussian(), z)); z += 0.004 }
            x += 0.004
        }
        // The object: a 12 x 5 x 8 cm block at the centre; a clutter block 6 cm away, both on the table.
        func block(_ c: SIMD2<Float>, _ h: SIMD3<Float>) {
            var a = -h.x
            while a <= h.x {
                var b = -h.z
                while b <= h.z {
                    pts.append(SIMD3(c.x + a, table(c.x + a, c.y + b) + 2 * h.y, c.y + b))
                    var y: Float = 0.004
                    while y < 2 * h.y {
                        if abs(abs(a) - h.x) < 0.003 || abs(abs(b) - h.z) < 0.003 { pts.append(SIMD3(c.x + a, table(c.x + a, c.y + b) + y, c.y + b)) }
                        y += 0.004
                    }
                    b += 0.004
                }
                a += 0.004
            }
        }
        block(SIMD2(0, 0), SIMD3(0.06, 0.025, 0.04))
        block(SIMD2(0.15, 0.12), SIMD3(0.03, 0.02, 0.03))
        let box = TrackBox(center: SIMD3(0, tableY - 0.02 + 0.045, 0), half: SIMD3(0.2, 0.045, 0.19), yaw: 0)
        return (FlipCloud(points: pts, colors: pts.map { _ in SIMD3(repeating: 0.5) }, box: box), SIMD3(0.12, 0.05, 0.08))
    }

    @Test func cutsTheLeaningTableAndDrawsATightBox() throws {
        let (raw, size) = Self.scene()
        let (obj, rep) = try #require(FlipIsolation.isolate(raw))
        #expect(abs(rep.tiltDeg - 2) < 0.5)
        #expect(abs(rep.tableY - 0.75) < 0.003)
        #expect(abs(rep.floorToTableMM - 20) < 3)
        #expect(obj.points.allSatisfy { abs($0.x) < 0.07 && abs($0.z) < 0.05 })             // the clutter is gone
        #expect(obj.points.allSatisfy { $0.y > 0.75 + tan(Float(2 * Double.pi / 180)) * $0.x + 0.002 })   // and the table
        #expect(abs(obj.box.planeY - 0.75) < 0.004)
        let e = obj.box.half * 2
        #expect(abs(max(e.x, e.z) - size.x) < 0.03 && abs(min(e.x, e.z) - size.z) < 0.03)
    }

    @Test func theFlipLogIsSmallAndSaysWhichCheckRefused() throws {
        var log = FlipLog()
        log.main = FlipIsolation.Report(inputPoints: 109_246, objectPoints: 15_854, tableY: -0.3117, tiltDeg: 2.77, cutMM: 28,
                                        floorToTableMM: 23.3, extent: SIMD3(0.165, 0.073, 0.131))
        let r = FlipAligner.Result(toSegment0: matrix_identity_float4x4, quality: 0.2, overlap: 0.12, rmsMM: 2.3, inliers: 181,
                                   margin: 0.27, flippedDegrees: 127.6, refused: "overlap", ms: 900)
        for k in 1...10 { log.add(attempt: k, result: k == 3 ? nil : r, points: 2400, agrees: false) }
        let c = try #require(log.contract)
        #expect(c.attempts == 10 && c.last?.count == FlipLog.keep && c.last?.last?.refused == "overlap")
        #expect(c.main?.floorToTableMm == 23.3 && c.bottom == nil)
        let bytes = try JSON.encoder.encode(c).count
        #expect(bytes < 2000)
    }
}
