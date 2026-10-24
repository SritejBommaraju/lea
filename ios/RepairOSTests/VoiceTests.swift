import Foundation
import Testing
@testable import RepairOS

struct CueLimiterTests {
    @Test func firstCueSpeaksAtOnce() {
        #expect(CueLimiter().decide("Move closer here.", now: 0, busy: false) == .speak)
    }

    @Test func atMostOneUtteranceEveryFourSeconds() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 10)
        #expect(l.decide("Keep it in frame.", now: 11, busy: false) == .wait(until: 14))
        #expect(l.decide("Keep it in frame.", now: 13.99, busy: false) == .wait(until: 14))
        #expect(l.decide("Keep it in frame.", now: 14, busy: false) == .speak)
    }

    @Test func neverOverAnotherUtterance() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 0)
        #expect(l.decide("Keep it in frame.", now: 5, busy: true) == .wait(until: 5.5))
        #expect(l.decide("Keep it in frame.", now: 5, busy: false) == .speak)
    }

    @Test func neverTheSameLineTwiceInTenSeconds() {
        var l = CueLimiter()
        l.spoke("Move closer here.", at: 0)
        #expect(l.decide("Move closer here.", now: 5, busy: false) == .drop)
        #expect(l.decide("Move closer here.", now: 9.9, busy: false) == .drop)
        #expect(l.decide("Move closer here.", now: 10, busy: false) == .speak)
    }

    @Test func otherSpeechCountsForTheGapButNotForRepeats() {
        var l = CueLimiter()
        l.spoke(nil, at: 0)   // the report summary or an answer
        #expect(l.decide("Got it.", now: 2, busy: false) == .wait(until: 4))
        #expect(l.lastSaid.isEmpty)
    }

    @Test func oldLinesAreForgotten() {
        var l = CueLimiter()
        l.spoke("A", at: 0)
        l.spoke("B", at: 11)
        #expect(Set(l.lastSaid.keys) == ["B"])
    }
}

struct VoiceCueMappingTests {
    @Test func importantCuesAreSpokenBriefly() {
        #expect(VoiceCues.line(for: "Move closer here") == "Move closer here.")
        #expect(VoiceCues.line(for: "Other side") == "Now the other side.")
        #expect(VoiceCues.line(for: "Keep the object in frame") == "Keep it in frame.")
        #expect(VoiceCues.line(for: "All sides covered") == "All sides covered. Tap Done.")
        #expect(VoiceCues.line(for: "Scan saved") == "Got it.")
        for line in VoiceCues.spoken.values { #expect(line.split(separator: " ").count <= 6, "\(line)") }
    }

    @Test func steadyCuesStaySilent() {
        for cue in ["Walk slowly around it", "Keep walking around it", "Hold still a moment", "Move slowly",
                    "Point at the object", "Finding the object…", nil] {
            #expect(VoiceCues.line(for: cue) == nil)
        }
    }

    /// Every spoken cue is one the capture flow really shows (its style table, the dome's "Other side", or the
    /// scan screen's own "All sides covered" / "Scan saved").
    @Test func spokenCuesAreRealCues() {
        // "Move closer" is the prefix of QualityGate's "Move closer: about N cm away" (said by its first part).
        let shown = Set(CueStyle.table.keys).union(["Other side", "All sides covered", "Scan saved", "Move closer"])
        for cue in VoiceCues.spoken.keys { #expect(shown.contains(cue), "\(cue) is never shown") }
    }

    @Test func fixedLinesAreUnique() {
        #expect(VoiceCues.fixedLines.count == Set(VoiceCues.spoken.values).count)
    }
}

struct ReportSpeechTests {
    static func report(_ verdict: String, safety: String? = nil, extra: [String: Any] = [:]) -> ReportModel {
        var r: [String: Any] = ["verdict": verdict,
                                "findings": [["finding_id": "f1", "part_class": "housing", "issue": "cracked",
                                              "part_words": "left corner near the hinge"]],
                                "most_likely": ["f1"]]
        if let safety { r["safety"] = [["code": "unplug_first", "text": safety]] }
        return ReportModel(json: r.merging(extra) { $1 })
    }

    @Test func verdictTopProblemThenSafety() {
        let s = ReportSpeech.lines(Self.report("potential_issues", safety: "Unplug it before opening. Then wait."))
        #expect(s == "I found likely problems. Most likely, the housing looks cracked, left corner near the hinge. "
                + "Safety first: unplug it before opening.")
    }

    @Test func looksGoodAndCantTell() {
        #expect(ReportSpeech.lines(Self.report("looks_good")) == "I didn't see any problems in these photos.")
        let r = Self.report("insufficient_evidence", extra: ["verdict_reason": ["text": "The photos are too blurry. More."]])
        #expect(ReportSpeech.lines(r) == "I couldn't tell from these photos. The photos are too blurry.")
    }

    @Test func noProblemsSeenIsNotAllClear() {
        let r = Self.report("no_problems_seen")
        #expect(ReportSpeech.lines(r, caveat: "The inside was not photographed. More.")
                == "Nothing looked wrong in what I could see clearly. The inside was not photographed.")
    }

    @Test func productBriefIsSpokenBriefly() {
        let b = ProductBrief(json: ["name": "Apple 20W USB-C adapter", "brand": "Apple", "category": "charger",
                                    "known_issues": [["area": "USB-C port", "why": "debris stops it seating"]]])
        #expect(b?.spoken == "This looks like Apple 20W USB-C adapter. Owners often report trouble with the USB-C port, so we'll look closely there.")
    }

    @Test func acronymsKeepTheirCase() {
        #expect(ReportSpeech.lowerFirst("USB socket") == "USB socket")
        #expect(ReportSpeech.lowerFirst("Housing") == "housing")
    }
}

struct PCMWAVTests {
    @Test func samplesOutOfAWAV() {
        var wav = Data("RIFF".utf8) + Data([0, 0, 0, 0]) + Data("WAVE".utf8)
        wav += Data("fmt ".utf8) + Data([16, 0, 0, 0]) + Data(count: 16)
        let pcm = Data([1, 2, 3, 4, 5, 6])
        wav += Data("data".utf8) + Data([6, 0, 0, 0]) + pcm
        #expect(PCMPlayer.pcm(fromWAV: wav) == pcm)
        #expect(PCMPlayer.pcm(fromWAV: Data("nope".utf8)) == nil)
    }
}

@MainActor
struct WakePhraseTests {
    @Test func tolerantMatching() {
        #expect(WakeListener.afterWake("Hey Lea") == "")
        #expect(WakeListener.afterWake("hey leah, is it safe to open?") == "is it safe to open")
        #expect(WakeListener.afterWake("Hi Lia what's wrong with it") == "what's wrong with it")
        #expect(WakeListener.afterWake("okay so hey Leia it's broken") == "it's broken")
    }

    @Test func otherSpeechIsIgnored() {
        #expect(WakeListener.afterWake("they leave it on the table") == nil)
        #expect(WakeListener.afterWake("hello there") == nil)
        #expect(WakeListener.afterWake("") == nil)
    }
}

/// Answers to Lea's own question: a plain yes takes the offer on screen, and a one-word yes or no is a turn even in
/// the follow-up window (where a single stray word is not).
@MainActor
struct ShortAnswerTests {
    @Test func aPlainYesTakesTheOffer() {
        for yes in ["Yes.", "yeah", "Yeah, let's do it", "okay go ahead", "sure please", "yes Lea"] {
            #expect(LeaConversation.isYes(yes), "\(yes)")
        }
        for not in ["yes but it smells burnt", "no", "what would the scan show?", "maybe later"] {
            #expect(!LeaConversation.isYes(not), "\(not)")
        }
    }

    @Test func oneWordAnswersAreTurns() {
        #expect(WakeListener.isShortAnswer("Yes."))
        #expect(WakeListener.isShortAnswer("nope"))
        #expect(!WakeListener.isShortAnswer("um"))
        #expect(!WakeListener.isShortAnswer("television"))
    }
}
