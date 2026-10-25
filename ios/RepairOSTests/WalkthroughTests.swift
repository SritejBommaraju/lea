import Foundation
import simd
import Testing
@testable import RepairOS

/// "Walk me through it" (Report/Stage/Walkthrough): the steps in order, what is left out, every word from the
/// report's own fields; the voice "stop"; the camera path between two steps' views.
struct WalkthroughTests {
    /// A report with every section: two located top problems and one without a spot, another issue, two setup
    /// items, gaps (never a step), two safety notes, a next step and the object.
    static func full() -> ReportModel {
        func finding(_ id: String, _ part: String, _ issue: String, words: String? = nil, located: Bool = true,
                     severity: String = "functional", likelihood: String? = nil, obs: String? = nil,
                     category: String? = nil, fix: String? = nil) -> [String: Any] {
            var f: [String: Any] = ["finding_id": id, "part_class": part, "issue": issue, "severity": severity,
                                    "confidence_word": "medium"]
            let crop: [String: Any] = ["crop_ref": "blob://sha256:\(id)crop", "still_id": "scan_ab12_s3"]
            f["crop"] = crop
            var evidence: [String] = ["\(part) in still scan_ab12_s3 looks \(issue)"]
            if let obs { evidence.append("vision model (m1): \(obs)") }
            f["evidence"] = evidence
            let computed: [String: Any] = ["status": "computed", "point_m": [0.1, 0.2, 0.3]]
            let missing: [String: Any] = ["status": "not_computed", "reason": "no depth"]
            f["location"] = located ? computed : missing
            if let words { f["part_words"] = words }
            if let likelihood {
                let judgment: [String: Any] = ["likelihood_word": likelihood, "rank": 1, "verdict": "suspect"]
                f["judgment"] = judgment
            }
            if let category { f["category"] = category }
            if let fix { f["fix"] = fix }
            return f
        }
        let json: [String: Any] = [
            "verdict": "potential_issues",
            "object": ["category": "toaster_oven", "brand": "Acme"],
            "most_likely": ["f_wire", "f_lid", "f_fan"],
            "findings": [
                finding("f_wire", "wire", "loose", words: "red wire above the fan motor", severity: "safety", likelihood: "likely",
                        obs: "The red wire hangs free with a bare end. The photo is a little blurry."),
                finding("f_lid", "lid hinge", "cracked", words: "near the back left corner", likelihood: "unsure"),
                finding("f_fan", "fan_blade", "bent", located: false),
                finding("f_knob", "knob", "scratched", severity: "cosmetic"),
            ],
            "setup": [finding("f_bat", "battery", "missing", category: "setup", fix: "Put the batteries in, then test again."),
                      finding("f_plug", "power plug", "unseated", category: "setup", fix: "Push the plug fully into the socket.")],
            "insufficient_evidence": [["finding_id": "g1", "part_class": "screw", "reason": "could not judge the screw in still scan_ab12_s8; the crop is too blurry"],
                                      ["finding_id": "g2", "part_class": "unknown_part", "reason": "region too small in still s5"]],
            "not_inspected": [["finding_id": "g3", "part_class": "region", "reason": "past this photo's limit of the strongest detector regions"]],
            "safety": [["code": "unplug_first", "text": "Unplug the device before you open it. Capacitors can hold a charge."],
                       ["code": "heat_damage", "text": "The heating element gets hot."]],
            "next_step": ["kind": "request_closeup", "instruction": "Take a close, sharp photo of the red wire. Hold the phone steady."],
        ]
        return ReportModel(json: json)
    }

    @Test func stepsFollowTheReportInOrder() {
        let steps = Walkthrough.steps(Self.full())
        #expect(steps.map(\.kind) == [.intro, .problem, .problem, .problem, .others, .setup, .setup, .safety, .outro])   // a closer-photo request is no step
        #expect(steps.map(\.id).filter { $0.hasPrefix("problem:") } == ["problem:f_wire", "problem:f_lid", "problem:f_fan"])
        #expect(steps[1].number == 1 && steps[3].number == 3)
        #expect(Set(steps.map(\.id)).count == steps.count)
    }

    @Test func locatedProblemsFlyAndTheRestAreStillNarratedWithTheirPhoto() {
        let steps = Walkthrough.steps(Self.full())
        #expect(steps[1].focus == "f_wire" && steps[2].focus == "f_lid")
        #expect(steps[3].focus == nil)                                   // no spot on the scan: no flight
        #expect(steps[3].speech.contains("the fan blade looks bent"))
        #expect(steps[3].crops == ["f_fancrop"])                         // its evidence photo still shows
        #expect(steps.filter { $0.kind != .problem }.allSatisfy { $0.focus == nil })
        // A hero without 3D (or the photo hero's own rule) decides what counts as located.
        #expect(Walkthrough.steps(Self.full()) { _ in false }.allSatisfy { $0.focus == nil })
    }

    @Test func problemSaysWhatWhereWhyHowSure() {
        let s = Walkthrough.steps(Self.full())[1]
        #expect(s.title == "Wire may be loose")
        #expect(s.eyebrow == "Problem 1 of 3 · Safety")
        #expect(s.speech == "First, the wire may be loose. Look at the red wire above the fan motor. "
                + "In the photo, the red wire hangs free with a bare end. I think it's likely the cause. "
                + "It's from photos only, so it isn't proven.")
        let lid = Walkthrough.steps(Self.full())[2]
        #expect(lid.speech.contains("Second, the lid hinge looks cracked. It's near the back left corner."))
        #expect(lid.speech.contains("I'm not sure it's the cause."))
    }

    @Test func sureLineUsesTheReportsOwnWordsAndProofWins() {
        var p = ReportWordsTests.problem("Wire", "May be loose")
        #expect(Walkthrough.sureLine(p) == "I'm only partly sure about this one. It's from photos only, so it isn't proven.")
        p.likelihood = "Very unlikely"
        #expect(Walkthrough.sureLine(p).hasPrefix("I think it's very unlikely to be the cause."))
        p.proved = true
        #expect(Walkthrough.sureLine(p) == "A meter reading confirmed it.")
    }

    @Test func introOutroAndTheOtherSections() {
        let steps = Walkthrough.steps(Self.full())
        #expect(steps[0].title == "Toaster oven")
        #expect(steps[0].speech == "Let's walk through your toaster oven. I found three likely problems. One of them is a safety issue.")
        #expect(steps[4].speech == "One more thing is worth a look: the knob looks scratched.")
        #expect(steps[5].speech == "Something to set up, not broken: the battery looks missing. Put the batteries in, then test again.")
        #expect(steps[5].title == "Put the batteries in, then test again.")
        #expect(steps[6].speech.hasPrefix("Also something to set up, not broken: the power plug: not plugged in fully."))
        // What wasn't checked is not a step and not in the summary (no coverage caveat by default, owner 2026-10-25).
        #expect(steps[7].speech == "Safety first: unplug the device before you open it. Also, the heating element gets hot.")
        // A request for a closer photo is not a step and not in the summary (the owner, 2026-10-25).
        #expect(!steps.contains { $0.kind == .next })
        #expect(steps[8].speech == "To sum up, most likely the wire may be loose. Two things need setting up. That's the walkthrough.")
        #expect(!steps.contains { $0.title == "Take a closer photo" || $0.speech.lowercased().contains("closer photo") })
        #expect(!steps.contains { $0.speech.contains("spots") || $0.speech.contains("counted as fine") })
    }

    @Test func aGenericNextStepIsSaidInItsOwnWords() {
        var r = Self.full()
        r.nextStep = ReportModel.NextStep(kind: "report_finding", instruction: "Likely problem: the red wire (photo 8). Unproven.")
        let steps = Walkthrough.steps(r)
        let next = steps.first { $0.kind == .next }
        #expect(next?.title == "Likely problem: the red wire (photo 8).")
        #expect(steps.last?.speech.contains("Start with the next step. That's the walkthrough.") == true)
        #expect(steps.last?.speech.contains("next step: next step") == false)
    }

    @Test func emptySectionsAreLeftOut() {
        let r = ReportModel(json: ["verdict": "looks_good", "capture": ["stills": 9, "stills_inspected": 9]])
        let steps = Walkthrough.steps(r)
        #expect(steps.map(\.kind) == [.intro, .outro])
        #expect(steps[0].speech == "Let's walk through your scan. I didn't see any problems in these photos.")
        #expect(steps[1].speech == "To sum up, I didn't see any problems in these photos. That's the walkthrough.")
    }

    @Test func setupBeyondThreeIsCounted() {
        var r = ReportModel(json: ["verdict": "looks_good"])
        r.setup = (1...5).map { i in
            var p = ReportWordsTests.problem("Part \(i)", "Missing")
            p.isSetup = true
            return p
        }
        let setup = Walkthrough.steps(r).filter { $0.kind == .setup }
        #expect(setup.count == 3)
        #expect(setup[2].speech.hasSuffix("There are two more in the report."))
        #expect(setup[0].eyebrow == "To set up · 1 of 5")
    }

    @Test func contractExampleReport() throws {
        let r = try ReportModelTests.example("report.json")
        let steps = Walkthrough.steps(r)
        #expect(steps.first?.kind == .intro && steps.last?.kind == .outro)
        #expect(!steps.contains { $0.kind == .next })   // its next step asks for a closer photo: not a tour step
        let problem = try #require(steps.first { $0.kind == .problem })
        #expect(problem.focus == nil)   // its location was not computed
        #expect(problem.speech.contains("In the photo, a gap is visible between the plug and its housing"))
    }

    @Test func wordingIsGroundedNeverInternal() {
        for step in Walkthrough.steps(Self.full()) + Walkthrough.steps(try! ReportModelTests.example("report.json")) {
            let text = step.title + " " + step.speech
            #expect(!text.contains("scan_"), "\(step.id): \(text)")
            #expect(!text.contains("_"), "\(step.id): \(text)")
            #expect(!text.contains("vision model"), "\(step.id): \(text)")
            #expect(!text.contains("%"), "\(step.id): \(text)")
            #expect(text.range(of: #"\bs\d+\b"#, options: .regularExpression) == nil, "\(step.id): \(text)")
        }
    }

    @Test func whereLineReadsAsASentence() {
        #expect(Walkthrough.whereLine("Near the left corner") == "It's near the left corner.")
        #expect(Walkthrough.whereLine("the round metal part") == "Look at the round metal part.")
        #expect(Walkthrough.whereLine("Blue wire next to the terminal") == "Look at the blue wire next to the terminal.")
        #expect(Walkthrough.whereLine("  ") == nil && Walkthrough.whereLine(nil) == nil)
    }

    @Test func stopByVoice() {
        // Lea quiet: the phrase alone, with or without her name.
        #expect(Walkthrough.isStop("Stop", leaSpeaking: false))
        #expect(Walkthrough.isStop("hey Lea, that's enough", leaSpeaking: false))
        #expect(Walkthrough.isStop("ok stop please", leaSpeaking: false))
        #expect(!Walkthrough.isStop("stop the motor first", leaSpeaking: false))
        #expect(!Walkthrough.isStop("what does that mean", leaSpeaking: false))
        // Lea speaking (the mic hears her too): only her name with the phrase.
        #expect(!Walkthrough.isStop("Stop", leaSpeaking: true))
        #expect(!Walkthrough.isStop("unplug it and stop", leaSpeaking: true))
        #expect(Walkthrough.isStop("the red wire hangs free lea stop", leaSpeaking: true))
        #expect(Walkthrough.isStop("in the photo stop Leah", leaSpeaking: true))
    }

    @MainActor @Test func stopEndsTheTourAndOtherWordsDoNot() {
        let tour = WalkthroughPlayer()
        tour.start(Walkthrough.steps(Self.full()))
        #expect(tour.active && WalkthroughPlayer.current === tour)
        #expect(!tour.heard("what does that mean", leaSpeaking: false))
        #expect(tour.active)
        #expect(tour.heard("Lea, stop", leaSpeaking: true))
        #expect(!tour.active && !tour.playing && WalkthroughPlayer.current == nil)
    }

    @Test func mutedCardsStayUpForAboutTheirReadingTime() {
        #expect(WalkthroughPlayer.readingTime("Short.") == .milliseconds(2500))
        #expect(WalkthroughPlayer.readingTime(String(repeating: "a", count: 90)) == .milliseconds(6000))
        #expect(WalkthroughPlayer.readingTime(String(repeating: "a", count: 900)) == .milliseconds(9000))
    }

    /// Greptile PR #32: pressing talk pauses the tour whatever it is doing (here its lead, before any line), so it
    /// never starts a card or moves on while the person records.
    @MainActor @Test func pressingTalkPausesTheTourBetweenLines() {
        let tour = WalkthroughPlayer()
        tour.start(Walkthrough.steps(Self.full()))
        #expect(tour.playing)
        let convo = LeaConversation(scanID: nil, gateway: GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil), diag: nil)
        convo.personStarted()   // what a tap on the talk button does first
        #expect(tour.active && !tour.playing && tour.index == 0)
        tour.stop()
    }

    /// Greptile PR #32: once a question has paused the tour, the conversation's answer is not the tour's to stop:
    /// neither swiping to another card nor the tour pausing (its interruption branch) cuts it off.
    @MainActor @Test func theTourNeverCutsOffTheAnswer() {
        let tour = WalkthroughPlayer()
        tour.start(Walkthrough.steps(Self.full()))
        let answer = LeaVoice.shared.beginStream(held: true)   // the answer to the person (held: nothing plays)
        tour.pause()
        #expect(LeaVoice.shared.utteranceID == answer)
        tour.next()
        #expect(tour.index == 1 && LeaVoice.shared.utteranceID == answer)
        tour.back()
        tour.stop()
        #expect(LeaVoice.shared.utteranceID == answer)
        LeaVoice.shared.endStreamNow(answer)
    }

    /// Greptile PR #32: a meter reading that ruled a fault out is not "confirmed", and a cleared fault is never a
    /// problem: with every finding ruled out (`most_likely` empty) the tour narrates no problems.
    @Test func ruledOutFaultsAreNeverNarratedAsProblems() {
        func finding(_ id: String, _ status: String) -> [String: Any] {
            ["finding_id": id, "part_class": "fuse", "issue": "blown", "severity": "functional", "confidence_word": "medium",
             "evidence": ["fuse in still scan_ab12_s3 looks blown"], "proof": ["status": status, "reading_id": "r_\(id)"] as [String: Any]]
        }
        let cleared = ReportModel(json: ["verdict": "no_problems_seen", "most_likely": [String](),
                                         "findings": [finding("f1", "ruled_out_by_meter"), finding("f2", "ruled_out_by_meter")]])
        #expect(cleared.mostLikely.isEmpty && cleared.otherIssues.isEmpty)
        let steps = Walkthrough.steps(cleared)
        #expect(!steps.contains { $0.kind == .problem || $0.kind == .others })
        #expect(!steps.map(\.speech).joined(separator: " ").contains("confirmed"))

        let mixed = ReportModel(json: ["verdict": "potential_issues",
                                       "findings": [finding("f1", "ruled_out_by_meter"), finding("f2", "confirmed_by_meter")]])
        #expect(mixed.mostLikely.map(\.id) == ["f2"] && mixed.otherIssues.isEmpty)
        #expect(mixed.mostLikely[0].proved && !mixed.mostLikely[0].ruledOut)
        #expect(Walkthrough.sureLine(mixed.mostLikely[0]) == "A meter reading confirmed it.")

        var p = ReportWordsTests.problem("Fuse", "Blown")
        p.ruledOut = true
        #expect(Walkthrough.sureLine(p) == "A meter reading ruled it out, so it isn't the problem.")
    }

    // MARK: Camera

    static let center = SIMD3<Float>(0, 0, 0)
    static let front = TourPose(eye: SIMD3(0, 0, 1), look: center)
    static let side = TourPose(eye: SIMD3(1, 0, 0), look: SIMD3(0.1, 0, 0))
    static let back = TourPose(eye: SIMD3(-0.01, 0, -1), look: center)

    @Test func cameraPathEndsOnEachStepsView() {
        let a = TourCamera.blend(Self.front, Self.side, 0, around: Self.center)
        let b = TourCamera.blend(Self.front, Self.side, 1, around: Self.center)
        #expect(simd_distance(a.eye, Self.front.eye) < 1e-4 && simd_distance(b.eye, Self.side.eye) < 1e-4)
        #expect(simd_distance(b.look, Self.side.look) < 1e-4)
    }

    @Test func cameraSwingsRoundTheObjectNeverThroughIt() {
        // Front to back: halfway it is beside the object at the same distance, not at its centre.
        for t in stride(from: Float(0), through: 1, by: 0.1) {
            let p = TourCamera.blend(Self.front, Self.back, t, around: Self.center)
            #expect(abs(simd_length(p.eye) - 1) < 1e-3)
        }
        let mid = TourCamera.blend(Self.front, Self.side, 0.5, around: Self.center)
        #expect(abs(mid.eye.x - mid.eye.z) < 1e-3 && mid.eye.x > 0.6)   // the short way round (+x side, not −x)
    }

    @Test func partialSwipeMovesTheCameraPartway() throws {
        let poses = [Self.front, Self.side, Self.back]
        let quarter = try #require(TourCamera.pose(poses, at: 0.25, around: Self.center))
        #expect(quarter.eye.x > 0.01 && quarter.eye.x < 0.5)
        #expect(TourCamera.pose(poses, at: 1, around: Self.center) == Self.side)
        #expect(TourCamera.pose(poses, at: 9, around: Self.center) == Self.back)        // clamped
        #expect(TourCamera.pose(poses, at: 0.6, around: Self.center, cut: true) == Self.side)   // Reduce Motion: a cut
        #expect(TourCamera.pose([], at: 0, around: Self.center) == nil)
    }

    @Test func ringsFadeInAsTheCameraArrives() {
        let focuses: [String?] = [nil, "a", "b", nil]
        let w = TourCamera.weights(focuses, at: 1.25)
        #expect(w["a"] == 0.75 && w["b"] == 0.25)
        #expect(TourCamera.weights(focuses, at: 0).values.allSatisfy { $0 == 0 })
        #expect(TourCamera.weights(focuses, at: 1.4, cut: true) == ["a": 1, "b": 0])
        #expect(TourFrame(focuses: focuses, progress: 2.6).nearest == 3)
    }
}
