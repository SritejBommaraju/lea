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
        #expect(ReportWords.sentence(r) == "Three likely problems, one of them a safety problem. The first was seen 2 times.")
        #expect(ReportWords.tone(r) == .danger)
    }

    @Test func issuesWithoutAJoinWordUseAColon() {
        #expect(ReportWords.phrase(Self.problem("Cover", "Colour doesn't match")) == "Cover: colour doesn't match")
        #expect(ReportWords.phrase(Self.problem("LED", "Looks dim")) == "LED looks dim")
    }

    @Test func nothingWrongIsQuietAboutGapsButNeverCallsThemFine() {
        // No coverage caveat by default (owner, 2026-10-25): the gaps stay in the model for "What wasn't checked",
        // and no word on the default path says they were fine.
        let r = Self.report("looks_good", gaps: 12)
        #expect(ReportWords.eyebrow(r) == "Nothing looked wrong")
        #expect(ReportWords.sentence(r) == nil)
        #expect(ReportWords.unchecked(r) == 12)
        #expect(!ReportWords.headline(r).lowercased().contains("fine"))
        #expect(ReportWords.tone(r) == .good)
    }

    @Test func countsSentenceSaysHowManyAreSafety() {
        let one = Self.report("potential_issues", likely: [Self.problem("Wire", "May be loose", severity: .safety)])
        #expect(ReportWords.sentence(one) == "One likely problem, a safety problem.")
        let two = Self.report("potential_issues", likely: [Self.problem("Wire", "May be loose"), Self.problem("Lid", "Looks bent")],
                              other: [Self.problem("Knob", "Looks scratched")])
        #expect(ReportWords.sentence(two) == "Two likely problems. One more thing is worth a look.")
    }

    @Test func noProblemsSeenIsCalmAndQuiet() {
        var r = Self.report("no_problems_seen", gaps: 11)
        r.photosInspected = 13
        r.verdictCaveat = "Nothing looked wrong in the 2 areas Lea could see clearly. 11 areas need a closer photo."
        #expect(r.verdict == .noProblemsSeen)
        #expect(ReportWords.headline(r) == "No problems seen")
        #expect(ReportWords.eyebrow(r) == "13 photos checked")
        #expect(ReportWords.tone(r) == .good)
        #expect(ReportWords.sentence(r) == nil)   // the caveat is off the default path (owner, 2026-10-25)
        // Voice/ReportSpeech owns the spoken wording: first person, never "fine", and no caveat on landing.
        let spoken = ReportSpeech.lines(r)
        #expect(spoken == "Nothing looked wrong in what I could see clearly.")
        #expect(!spoken.lowercased().contains("fine"))
        r.verdictCaveat = nil
        #expect(ReportWords.sentence(r) == nil)
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
        #expect(RecreationStage.ring(rgb: SIMD3(0.9, 0.3, 0.2)) != nil)
        #expect(RecreationStage.ring(rgb: FixedHex.rgb(FixedHex.next)) != nil)
    }
}

/// What Lea says when the object is identified during the scan (Capture/CaptureView IdentitySpeech).
@MainActor
struct IdentitySpeechTests {
    typealias I = LiveEvents.Identity
    static let guess = I(category: "Microcontroller development board", brand: nil, model: nil, confidence: "medium")
    static let named = I(category: "Microcontroller development board", brand: "ELEGOO", model: "UNO R3", confidence: "high")

    @Test func firstGuessThenTheNamedProductOnce() {
        let said = IdentitySpeech.Said()
        #expect(said.next(Self.guess, corrected: nil) == "This looks like a microcontroller development board.")
        said.spoke()
        #expect(said.next(Self.named, corrected: nil) == "It's an ELEGOO UNO R3.")
        said.spoke()
        #expect(said.next(Self.named, corrected: nil) == nil)                                 // never twice
        #expect(said.next(I(category: "air fryer", confidence: "low"), corrected: nil) == nil)   // no flip-flopping
        #expect(IdentitySpeech.Said().next(I(category: "air fryer", confidence: "medium"), corrected: nil) == "This looks like an air fryer.")
    }

    /// Greptile PR #31: a medium guess that already carried the brand was said as its category only, so the
    /// high-confidence name that follows is still said.
    @Test func aBrandSaidAsItsCategoryStillGetsNamed() {
        let said = IdentitySpeech.Said()
        let branded = I(category: "Microcontroller development board", brand: "ELEGOO", model: "UNO R3", confidence: "medium")
        #expect(said.next(branded, corrected: nil) == "This looks like a microcontroller development board.")
        said.spoke()
        #expect(said.level == .category)
        #expect(said.next(Self.named, corrected: nil) == "It's an ELEGOO UNO R3.")
    }

    /// Only what started playing counts: a line that was offered but replaced by a newer guess is not "said".
    @Test func onlyWordsThatPlayedCount() {
        let said = IdentitySpeech.Said()
        _ = said.next(Self.guess, corrected: nil)               // waiting for the voice gap, never played
        #expect(said.level == .nothing)
        #expect(said.next(Self.named, corrected: nil) == "It's an ELEGOO UNO R3.")   // the newest guess, at play time
        said.spoke()
        #expect(said.level == .product)
        #expect(said.next(Self.guess, corrected: nil) == nil)
    }

    /// Greptile PR #31: the words are worked out when they play, so a correction saved while the line waited
    /// silences the rejected guess.
    @Test func aCorrectionSilencesTheWaitingGuess() {
        let said = IdentitySpeech.Said()
        #expect(said.next(Self.guess, corrected: "Arduino clone") == nil)
        said.spoke()
        #expect(said.level == .nothing)
        #expect(IdentitySpeech.line(Self.named, said: .category, corrected: "Arduino clone") == nil)
    }
}

/// Greptile PR #31: a reopened report never polls the pipeline; its cached choice of photos still keeps the
/// recreation (a blob for a small object) off the stage.
struct StageHeroChoiceTests {
    @Test func aSavedReportStillHonoursPhotos() {
        let cached = ReportModel(json: ["verdict": "no_issues_found", "mesh": ["hero": "photos", "rev": 2] as [String: Any]])
        let hero = ReportView.heroChoice(live: nil, pipeline: nil, cached: cached.hero)
        #expect(hero == "photos")
        #expect(!ReportView.showsRecreation(hero: hero, available: true))
        #expect(ReportView.showsRecreation(hero: ReportView.heroChoice(live: nil, pipeline: nil, cached: "mesh"), available: true))
        #expect(!ReportView.showsRecreation(hero: "mesh", available: false))
        #expect(ReportView.heroChoice(live: "mesh", pipeline: "photos", cached: "photos") == "mesh")   // live first
    }
}

/// The owner's small-board scan (2026-10-25): `mesh.hero = "photos"`, a 342-triangle mesh, nothing looked wrong. The
/// phone looked "still loading": the photo hero showed only the numbered findings' stills, and with none it waited
/// forever. Every path must end on a picture (or a plain line), show what the object is, and keep the caveat off.
struct PhotoHeroPathTests {
    static let report: ReportModel = ReportModel(json: [
        "verdict": "no_problems_seen",
        "verdict_reason": ["text": "No issues seen.",
                           "caveat": "Nothing looked wrong in the 4 areas Lea could see clearly. 18 areas need a closer photo."],
        "object": ["category": "microcontroller_board", "brand": "Acme", "model": "Board 3"],
        "mesh": ["hero": "photos", "rev": 1] as [String: Any],
        "capture": ["stills": 22, "stills_inspected": 22],
    ])

    @Test func photosHeroNeverWaitsOnFindings() throws {
        let r = Self.report
        #expect(r.mostLikely.isEmpty && StageMarks.photoMarks(r).isEmpty)
        // The backend chose photos: no recreation, the photo hero.
        #expect(!ReportView.showsRecreation(hero: ReportView.heroChoice(live: nil, pipeline: nil, cached: r.hero), available: true))
        let dir = FileManager.default.temporaryDirectory.appending(path: "photo-hero-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        #expect(ModelSource.find(in: dir, hero: "photos").isPhotos)
        // With nothing numbered, the hero falls back to the scan's own stills (so it always has a picture to show).
        #expect(PhotoHero.heroStills(marked: [], available: ["s3", "s1"]) == ["s3", "s1"])
        let mark = PhotoHero.Mark(id: "f", number: 1, still: "s7", box: nil, tone: .caution)
        #expect(PhotoHero.heroStills(marked: [mark], available: ["s3"]) == ["s7"])
        #expect(PhotoHero.heroStills(marked: [], available: []).isEmpty)   // the hero then says so, never spins
    }

    @Test func stillsAreListedLargestFirst() async throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "photo-stills-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        try Data(count: 10).write(to: dir.appending(path: "scan_ab_s1.jpg"))
        try Data(count: 30).write(to: dir.appending(path: "scan_ab_s2.jpg"))
        try Data(count: 5).write(to: dir.appending(path: "notes.txt"))
        #expect(await PhotoHero.stillIDs(in: dir) == ["scan_ab_s2", "scan_ab_s1"])
    }

    @Test func identityShowsAndTheCaveatStaysOff() {
        let r = Self.report
        #expect(ReportView.identity(corrected: nil, report: r.objectWords, live: nil) == "Microcontroller board · Acme Board 3")
        #expect(ReportView.identity(corrected: nil, report: "Microcontroller board", live: "Microcontroller board · Acme Board 3")
                == "Microcontroller board · Acme Board 3")
        #expect(ReportView.identity(corrected: "My board", report: r.objectWords, live: nil) == "My board")
        #expect(ReportWords.sentence(r) == nil)
        #expect(ReportWords.headline(r) == "No problems seen")
        #expect(!ReportSpeech.lines(r).contains("areas"))
    }
}

/// The person never sees numbered photos, so no photo number reaches the screen (owner, 2026-10-25).
struct DisplayWordsTests {
    @Test func photoNumbersNeverReachTheScreen() {
        #expect(ReportWords.display("Take a close photo of the contact beside the label (photo 8). Hold steady.")
                == "Take a close photo of the contact beside the label. Hold steady.")
        #expect(ReportWords.display("The crack shows in photo 3 and on the lid (photos 4 and 5).") == "The crack shows and on the lid.")
        #expect(ReportWords.display("could not judge the screw in still scan_ab12_s8") == "could not judge the screw")
        #expect(ReportWords.display("Photo 2 is blurry.") == "A photo is blurry.")
        #expect(ReportWords.display(ReportWords.gapReason(ReportWordsTests.gap("Screw", "too dark in still s4"), unclear: true)) == "Too dark")
        let none = ReportWords.display("No numbers here.")
        #expect(none == "No numbers here.")
    }

    @Test func theSummaryLeavesCoverageCaveatsOut() {
        let text = "Nothing looked wrong in the areas that could be seen clearly, but this is not a clean bill of health, "
            + "because some areas still need a closer photo. The battery, buttons, cable and grip housing all looked fine in "
            + "the photos. Two spots need a closer photo! The case is scuffed."
        #expect(ReportWords.withoutCaveats(text)
                == "The battery, buttons, cable and grip housing all looked fine in the photos. The case is scuffed.")
        #expect(ReportWords.withoutCaveats("The fan blade looks bent.") == "The fan blade looks bent.")
        #expect(ReportWords.withoutCaveats("Some areas aren't counted as fine.") == "")
    }

    @Test func captureRequestsNeverLead() {
        #expect(ReportWords.isCaptureRequest(ReportModel.NextStep(kind: "request_closeup", instruction: "x")))
        #expect(ReportWords.isCaptureRequest(ReportModel.NextStep(kind: "rescan", instruction: "x")))
        #expect(!ReportWords.isCaptureRequest(ReportModel.NextStep(kind: "run_voltage_test", instruction: "x")))
    }
}

/// Bottom-scan photos that could not be lined up with the model (report `bottom_not_placed`).
struct BottomPhotosTests {
    @Test func unplacedBottomFindingsBecomePhotosOncePerCrop() {
        let r = ReportModel(json: ["verdict": "no_problems_seen", "bottom_not_placed": [
            ["finding_id": "f1", "segment": 1, "part_class": "battery_cover", "reason": "not aligned",
             "photo": ["still_id": "s20", "crop_ref": "blob://sha256:aaa"]],
            ["finding_id": "f2", "segment": 1, "part_class": "screw", "reason": "not aligned",
             "photo": ["still_id": "s20", "crop_ref": "blob://sha256:aaa"]],
            ["finding_id": "f3", "segment": 1, "reason": "not aligned", "photo": ["still_id": "s21", "crop_ref": "blob://sha256:bbb"]],
        ]])
        #expect(r.bottomPhotos.map(\.crop) == ["aaa", "bbb"])
        #expect(r.bottomPhotos.map(\.part) == ["Battery cover", "Bottom"])
        #expect(r.cropSHAs.contains("bbb"))
        #expect(ReportModel(json: ["verdict": "looks_good"]).bottomPhotos.isEmpty)
    }
}
