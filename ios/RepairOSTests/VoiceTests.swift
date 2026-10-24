import Foundation
import Testing
@testable import RepairOS

/// Lea's turn-taking (ported from VEL's speech.mjs and its endpoint rules, checked with VEL's own cases) and the
/// gateway's voice answers as the app decodes them.
struct VoiceTests {
    typealias T = TurnTaking

    @Test func echoBackchannelsAndStops() {
        #expect(T.isEcho(heard: "here is the magic nine plus sixteen", spoken: "Here's the magic. Nine plus sixteen is twenty-five. Here is the"))
        #expect(!T.isEcho(heard: "wait why is that true", spoken: "Here's the magic. Nine plus sixteen is twenty-five."))
        #expect(T.isBackchannel("Mm-hmm."))
        #expect(!T.isBackchannel("why is that"))
        #expect(T.isStop("hold on a second"))
        #expect(!T.isStop("stop the motor first"))
    }

    @Test func turnKindsAndReactions() {
        #expect(T.turnKind("Can you explain the Pythagorean theorem?") == .request)
        #expect(T.turnKind("Wait, why is the hypotenuse always the longest side?") == .question)
        #expect(T.turnKind("Okay, got it. Go on.") == .back)
        #expect(T.turnKind("I don't get it") == .confused)
        #expect(T.reaction(for: "Okay, go on.", recent: [], r: 0.9) == nil)
        #expect(T.reaction(for: "why does it work?", recent: [], r: 0.1) == nil)
    }

    @Test func resumingAndSentences() {
        let s = "Nine plus sixteen is twenty-five, so the two small squares fill the big one exactly."
        #expect(T.resumeLine(s, heard: 0.6, n: 0).from == 5)
        #expect(T.resumeLine(s, heard: 0.1, n: 0).from == 0)
        #expect(T.sentences("See this red line here? That's demand for coffee. When the price goes up, people buy less.").count == 3)
    }

    @Test func endOfTurnTiming() {
        #expect(T.hangover(after: "What did you find?") == 0.576)
        #expect(T.hangover(after: "so I measured it and") == 1.8)
        #expect(T.hangover(after: "hello") == 0.9)
    }

    @Test func shortAnswersReachLeaOnceSheStops() {
        #expect(T.takesTurn("Yes.", whileLeaTalks: false))      // "Can you scan it?" "Yes."
        #expect(T.takesTurn("yeah", whileLeaTalks: false))
        #expect(!T.takesTurn("yeah", whileLeaTalks: true))      // listening, not taking the turn
        #expect(!T.takesTurn("wait", whileLeaTalks: false))
        #expect(T.takesTurn("it hums but won't spin", whileLeaTalks: true))
    }

    @Test func onlyMicWordsOverLeasVoiceAreEcho() {
        let spoken = "Can you scan it?"
        #expect(T.dropAsEcho("can you scan it", spoken: spoken, fromMic: true, overPlayback: true))
        #expect(!T.dropAsEcho("can you scan it", spoken: spoken, fromMic: true, overPlayback: false))   // they said it
        #expect(!T.dropAsEcho("can you scan it", spoken: spoken, fromMic: false, overPlayback: true))   // typed
    }

    /// The thinking sounds depend on this: a slow reply must not hold the timer back.
    @Test func aSlowReplyLosesTheRaceAtOnce() async {
        let slow = Task { () -> Int in try? await Task.sleep(for: .seconds(5)); return 1 }
        let start = ContinuousClock.now
        let r = await firstWithin(slow, seconds: 0.1)
        #expect(r == nil)
        #expect(ContinuousClock.now - start < .seconds(1))
        slow.cancel()
        #expect(await firstWithin(Task { 7 }, seconds: 2) == .some(7))
    }

    @Test func gatewayRepliesDecode() throws {
        let json = #"{"id":"t1","reply":"Opening a new scan.","actions":[{"type":"scan","symptom":"stops after a minute"},{"type":"report","scan_id":"scan_1"}]}"#
        let r = try JSONDecoder().decode(VoiceReply.self, from: Data(json.utf8))
        #expect(r.reply == "Opening a new scan.")
        #expect(r.actions == [VoiceAction(type: "scan", symptom: "stops after a minute"), VoiceAction(type: "report", scanID: "scan_1")])
    }
}
