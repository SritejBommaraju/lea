import CoreGraphics
import Foundation
import SceneKit
import Testing
@testable import RepairOS

/// The dark-stage report's words (Report/Stage/ReportWords) and marker separation (MarkerLayout).
struct ReportWordsTests {
    static func problem(_ part: String, _ issue: String, severity: ReportModel.Severity? = .functional, regions: Int = 1,
                        proved: Bool = false) -> ReportModel.Problem {
        ReportModel.Problem(id: part, part: part, issue: issue, whereWords: nil, severity: severity, confidence: "Medium confidence",
                            likelihood: nil, observation: nil, evidence: [], crops: [], photoLabels: [], regions: regions,
                            locationNote: nil, point: nil, proved: proved)
    }

    static func gap(_ part: String, _ reason: String, photo: String? = nil) -> ReportModel.Gap {
        ReportModel.Gap(id: UUID().uuidString, part: part, reason: reason, detail: nil, photoLabel: photo, crop: nil, regions: 1)
    }

    static func report(_ verdict: String, likely: [ReportModel.Problem] = [], other: [ReportModel.Problem] = [],
                       gaps: Int = 0) -> ReportModel {
        var r = ReportModel(json: ["verdict": verdict])
        r.mostLikely = likely
        r.otherIssues = other
        r.insufficient = (0..<gaps).map { _ in gap("Region", "could not judge the region in still s3") }
        return r
    }

    @Test func problemsLeadWithTheTopFinding() {
        let r = Self.report("potential_issues",
                            likely: [Self.problem("Wire", "May be loose", severity: .safety, regions: 2),
                                     Self.problem("Housing", "Looks cracked"), Self.problem("Cover", "Colour doesn't match")])
        #expect(ReportWords.eyebrow(r) == "3 likely problems · 1 safety")
        #expect(ReportWords.headline(r) == "Wire may be loose")
        #expect(ReportWords.sentence(r) == "Seen 2 times. Two more things are worth a look.")
        #expect(ReportWords.tone(r) == .danger)
    }

    @Test func issuesWithoutAJoinWordUseAColon() {
        #expect(ReportWords.phrase(Self.problem("Cover", "Colour doesn't match")) == "Cover: colour doesn't match")
        #expect(ReportWords.phrase(Self.problem("LED", "Looks dim")) == "LED looks dim")
    }

    @Test func nothingWrongStillCountsWhatWasNotChecked() {
        let r = Self.report("looks_good", gaps: 12)
        #expect(ReportWords.eyebrow(r) == "Nothing looked wrong · 12 spots unchecked")
        #expect(ReportWords.sentence(r) == "12 spots weren't clear enough to judge, so they aren't counted as fine.")
        #expect(ReportWords.tone(r) == .good)
    }

    @Test func noProblemsSeenIsCalmAndKeepsItsCaveat() {
        var r = Self.report("no_problems_seen", gaps: 11)
        r.photosInspected = 13
        r.verdictCaveat = "Nothing looked wrong in the 2 areas Lea could see clearly. 11 areas need a closer photo."
        #expect(r.verdict == .noProblemsSeen)
        #expect(ReportWords.headline(r) == "No problems seen")
        #expect(ReportWords.eyebrow(r) == "13 photos checked")
        #expect(ReportWords.tone(r) == .good)
        #expect(ReportWords.sentence(r) == r.verdictCaveat)
        // Voice/ReportSpeech owns the spoken wording: first person, then what the caveat leaves out.
        let spoken = ReportSpeech.lines(r)
        #expect(spoken.contains("Nothing looked wrong in what I could see clearly."))
        #expect(spoken.contains("11 areas need a closer photo."))
        #expect(!spoken.contains("Lea could see"))
        r.verdictCaveat = nil
        #expect(ReportWords.sentence(r) == "11 spots need a closer photo.")
    }

    @Test func notEnoughUsesTheSameWordsAsTheBigScreen() {
        #expect(ReportWords.eyebrow(Self.report("insufficient_evidence")) == "Not enough to call it yet")
    }

    @Test func gapSummaryIsHonestAndPlain() {
        #expect(ReportWords.gapSummary(notInspected: 7, unclear: 12)
                == "12 spots weren't clear enough and seven weren't looked at. They aren't counted as fine.")
        #expect(ReportWords.gapSummary(notInspected: 1, unclear: 0) == "One spot wasn't looked at. It isn't counted as fine.")
        #expect(ReportWords.gapSummary(notInspected: 0, unclear: 0) == "")
    }

    @Test func backendInternalsNeverReachTheScreen() {
        let s = ReportWords.sanitize("could not judge the unknown_part in still scan_799d9ab8d44e0e2b_s3; past this photo's limit of the strongest detector regions")
        #expect(!s.contains("_"))
        #expect(!s.contains("still"))
        #expect(s.contains("in photo 3"))
        #expect(ReportWords.gapReason(Self.gap("Small regions", "1 small region in still s1 was detected but not inspected: too small in this photo to judge"), unclear: false)
                == "Too small in photo 1")
        #expect(ReportWords.gapReason(Self.gap("Bell", "could not judge the bell in still scan_ab12_s11"), unclear: true)
                == "Not clear enough in photo 11")
        #expect(ReportWords.gapTitle(Self.gap("Unknown part", "x")) == "An area")
    }

    @Test func safetyLeadsWithTheInstruction() {
        let notes = [ReportModel.SafetyNote(code: "mains_power", text: "This device looks like it runs on wall power: keep it unplugged while it is open, and remember that stored charge can stay after unplugging.")]
        #expect(ReportWords.safetyLine(notes) == "Keep it unplugged while it is open.")
        let plain = [ReportModel.SafetyNote(code: "x", text: "Unplug the device before you open it.")]
        #expect(ReportWords.safetyLine(plain) == "Unplug the device before you open it.")
    }

    @Test func photoLabelsReadLongStillIDs() {
        #expect(ReportModel.photoLabel("scan_799d9ab8d44e0e2b_s3") == "Photo 3")
        #expect(ReportModel.photoLabel("s11") == "Photo 11")
    }
}

struct MarkerLayoutTests {
    @Test func theFirstMarkerNeverMovesAndOthersClear() {
        let pts = [CGPoint(x: 100, y: 100), CGPoint(x: 104, y: 100), CGPoint(x: 300, y: 300)]
        let o = MarkerLayout.separate(pts, gap: 34)
        #expect(o[0] == .zero)
        let p1 = CGPoint(x: pts[1].x + o[1].dx, y: pts[1].y + o[1].dy)
        #expect(hypot(p1.x - pts[0].x, p1.y - pts[0].y) >= 33.9)
        #expect(o[2] == .zero)
    }

    @Test func markersOnTheSameSpotFanOut() {
        let pts = Array(repeating: CGPoint(x: 50, y: 50), count: 3)
        let o = MarkerLayout.separate(pts, gap: 30)
        let placed = zip(pts, o).map { CGPoint(x: $0.x + $1.dx, y: $0.y + $1.dy) }
        for i in 0..<3 { for j in (i + 1)..<3 {
            #expect(hypot(placed[i].x - placed[j].x, placed[i].y - placed[j].y) >= 29.9)
        } }
    }
}

/// The problem area's size on the scan (ReportModel.extent) and the ring drawn round it.
struct ProblemRingTests {
    @Test func extentIsHalfTheRegionDiagonal() {
        let loc: [String: Any] = ["status": "computed", "point_m": [0, 0, 0],
                                  "region_min_m": [0, 0, 0], "region_max_m": [0.03, 0.04, 0]]
        #expect(abs((ReportModel.extent(loc) ?? 0) - 0.025) < 1e-6)
    }

    @Test func noRingWithoutAComputedPoint() {
        #expect(ReportModel.extent(["status": "failed", "region_min_m": [0, 0, 0], "region_max_m": [1, 1, 1]]) == nil)
        #expect(ReportModel.extent(["status": "computed", "point_m": [0, 0, 0]]) == nil)
    }

    @MainActor @Test func aBadgeWinsATapAndARingOnlyInsideItsCircle() {
        let stage = RecreationStage()
        let ring = SCNNode(geometry: SCNPlane(width: 0.026, height: 0.026))   // a 10 mm ring: 13 mm half-width plane
        ring.name = "ring:a"
        let badge = SCNNode(geometry: SCNPlane(width: 0.01, height: 0.01))
        badge.name = "marker:b"
        #expect(stage.markerID(atHit: [(ring, SIMD3(0.002, 0.003, 0))]) == "a")        // inside the circle
        #expect(stage.markerID(atHit: [(ring, SIMD3(0.0125, 0.0125, 0))]) == nil)      // a clear corner
        #expect(stage.markerID(atHit: [(ring, SIMD3(0.002, 0, 0)), (badge, nil)]) == "b")   // a badge first
    }

    @MainActor @Test func ringTextureDraws() {
        #expect(RecreationStage.ring(rgb: SIMD3(0.9, 0.3, 0.2), dashed: false) != nil)
        #expect(RecreationStage.ring(rgb: SIMD3(0.5, 0.5, 0.6), dashed: true) != nil)
    }
}

/// What Lea says when the object is identified during the scan (Capture/CaptureView IdentitySpeech).
struct IdentitySpeechTests {
    typealias I = LiveEvents.Identity
    @Test func firstGuessThenTheNamedProductOnce() {
        let guess = I(category: "Microcontroller development board", brand: nil, model: nil, confidence: "medium")
        let named = I(category: "Microcontroller development board", brand: "ELEGOO", model: "UNO R3", confidence: "high")
        #expect(IdentitySpeech.line(guess, after: nil) == "This looks like a microcontroller development board.")
        #expect(IdentitySpeech.line(named, after: guess) == "It's an ELEGOO UNO R3.")
        #expect(IdentitySpeech.line(named, after: named) == nil)                       // never twice
        #expect(IdentitySpeech.line(I(category: "air fryer", confidence: "low"), after: guess) == nil)   // no flip-flopping
        #expect(IdentitySpeech.line(I(category: "air fryer", confidence: "medium"), after: nil) == "This looks like an air fryer.")
    }
}
