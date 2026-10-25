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
}
