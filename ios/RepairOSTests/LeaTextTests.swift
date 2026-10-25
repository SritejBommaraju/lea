import Foundation
import Testing
@testable import RepairOS

/// The words of Lea's answers as the screen shows them (Voice/LeaText, LeaCaptionFeed.exchange,
/// LeaConversation.heardIndex). The answers are real recorded turns (eval/live_vision/results, 2026-10-25), never
/// invented text.
struct LeaTextTests {
    // Real turns, verbatim (`raw`: what the model wrote, markers included).
    static let xbHere = "You're right, the blue X is the one in the middle of the group, and the red B sits to its left. The scan couldn't judge the face buttons, though. The photos were too soft to show whether they're cracked or worn. <show part=\"ix_3\"/>The X button is the blue one, and <show part=\"ix_2\"/>the B button is the red one on the left. What's happening with them: are they not registering presses, or do they feel stuck?"
    static let knob = "The control knobs are on the steel front panel at the bottom of the open control assembly. There's a row of four round knobs running diagonally across the panel, and the panel is tilted open, so they're easy to see.\n\n<show part=\"f_493185e4858a\"/>\n<mark x=\"0.36\" y=\"0.61\" label=\"control knobs\"/>\n<mark x=\"0.65\" y=\"0.71\" label=\"knob row\"/>"
    static let whereX = "<show part=\"ix_3\"/> The X button is the blue one with the letter X. It's one of the four coloured face buttons, sitting between the green A and the yellow Y. On the controller it's the left-hand button of the diamond, and in this photo it's turned, so it looks like the middle one of the cluster."
    static let heldoutB = "The B button is the red one with the letter B, on the right side of the face buttons. In photo s2 it's the red button on the left of the four colored buttons, so it sits on the left side of the controller face, between the left thumbstick and the green A. Looking at the controller from the front, B is the red key just to the left of the X button cluster.\n\n<point photo=\"s2\" x=\"0.22\" y=\"0.55\" label=\"B button\"/>\n\nSince you said something isn't working, you can test it by pressing B in a game menu and seeing whether it registers. If it doesn't, tell me what's happening and we can narrow it down."
    static let sparse = "The clearest suspect is the small fan motor coil. <show part=\"f_36128a1bd09c\"/> It may have some brownish discoloration near its top left and a darkened end on the bobbin, which could be scorching, but the photos are soft there, so I can't confirm it. Also, <show part=\"f_3661beca1eaa\"/> the white wire near the lower right round metal part may have a frayed end. Nothing in these photos shows clear burn marks, but unplug it before you get close, and take a sharper close-up of the motor coil to check."

    // MARK: Markers, ids and image names never reach a caption

    @Test func markersAreStrippedAndTheSentenceStaysWhole() {
        let s = LeaText.sentences(Self.xbHere)
        #expect(s == [
            "You're right, the blue X is the one in the middle of the group, and the red B sits to its left.",
            "The scan couldn't judge the face buttons, though.",
            "The photos were too soft to show whether they're cracked or worn.",
            "The X button is the blue one, and the B button is the red one on the left.",
            "What's happening with them: are they not registering presses, or do they feel stuck?",
        ])
        #expect(!s.joined().contains("<") && !s.joined().contains("ix_"))
    }

    @Test func trailingMarkersLeaveNoEmptySentence() {
        let s = LeaText.sentences(Self.knob)
        #expect(s.count == 2)
        #expect(s.last == "There's a row of four round knobs running diagonally across the panel, and the panel is tilted open, so they're easy to see.")
        #expect(LeaText.clean("<show part=\"f_493185e4858a\"/>\n<mark x=\"0.36\" y=\"0.61\" label=\"control knobs\"/>") == "")
    }

    @Test func aLeadingMarkerLeavesNoSpaceOrLowerCase() {
        #expect(LeaText.sentences(Self.whereX).first == "The X button is the blue one with the letter X.")
        // A marker that opened a sentence after a comma ("Also, <show/> the white wire…") leaves one clean sentence.
        #expect(LeaText.sentences(Self.sparse)[2] == "Also, the white wire near the lower right round metal part may have a frayed end.")
    }

    @Test func anImageNameNeverShows() {
        // The real leak (2026-10-25 prefer-index run): "In photo s2 it's the red button…".
        let s = LeaText.sentences(Self.heldoutB)
        #expect(s[1].hasPrefix("It's the red button on the left of the four colored buttons"))
        #expect(!s.joined(separator: " ").contains("s2"))
        #expect(!s.joined(separator: " ").contains("<point"))
        #expect(s.count == 5)
    }

    @Test func aMarkerCutOffByTheEndOfTheStreamDoesNotLeak() {
        // The same real answer, cut part-way through its marker (a stream that stopped there).
        let cut = String(Self.heldoutB[..<Self.heldoutB.range(of: "label=")!.lowerBound])
        #expect(cut.hasSuffix("<point photo=\"s2\" x=\"0.22\" y=\"0.55\" "))
        #expect(!LeaText.sentences(cut).joined().contains("<"))
        #expect(LeaText.clean("<point photo=\"s2\" x=\"0.22\"") == "")
        // And the tail of one, when a cut fell inside it.
        #expect(LeaText.clean("part=\"ix_2\"/>the B button is the red one on the left.") == "The B button is the red one on the left.")
    }

    @Test func punctuationLeftByARemovedMarkerIsTidied() {
        // A marker or id between the words and the stop leaves ":." / " ," / "?." behind.
        #expect(LeaText.clean("It's the red one: <show part=\"ix_2\"/>.") == "It's the red one.")
        #expect(LeaText.clean("That's the part f_36128a1bd09c , near the motor.") == "That's the part, near the motor.")
        #expect(LeaText.clean("Is it this one ix_3?.") == "Is it this one?")
        #expect(LeaText.clean(", the white wire.") == "The white wire.")
        #expect(LeaText.clean("Wait...") == "Wait…")
        #expect(LeaText.clean(":.") == "")
    }

    @Test func decimalsAndAbbreviationsAreNotCut() {
        // The old caption splitter cut at every full stop: "1.5 mm" became "5 mm." on screen.
        #expect(LeaText.sentences("The gap is about 1.5 mm wide. Check it with a gauge.") ==
                ["The gap is about 1.5 mm wide.", "Check it with a gauge."])
        #expect(LeaText.sentences("Use a small tool, e.g. a spudger, to lift it. Then press.") ==
                ["Use a small tool, e.g. a spudger, to lift it.", "Then press."])
    }

    // MARK: Streaming: sentences in → captions out

    @Test func streamedSentencesBuildTheAnswerOnceEach() {
        // The gateway's sentence events for a real answer, then the same events again (a held answer replayed on
        // confirmation): every sentence is shown once.
        let events = LeaText.sentences(Self.xbHere)
        var shown: [String] = []
        for e in events + events {
            if let s = LeaText.shown(e, after: shown) { shown.append(s) }
        }
        #expect(shown == events)
    }

    @Test func aMarkerOnlyEventAndARepeatShowNothing() {
        let said = ["The X button is the blue one with the letter X."]
        #expect(LeaText.shown("<show part=\"ix_3\"/>", after: said) == nil)
        #expect(LeaText.shown("the X button is the blue one with the letter X", after: said) == nil)   // case, stop
        #expect(LeaText.shown("It's one of the four coloured face buttons.", after: said) == "It's one of the four coloured face buttons.")
    }

    // MARK: The strip: your question and the sentence you are hearing

    typealias Turn = LeaConversation.Turn

    @Test func theStripShowsTheSentenceBeingHeardNotTheNewestToArrive() {
        let said = LeaText.sentences(Self.xbHere)
        let q = Turn(mine: true, text: "the X button and the B button are here.")
        let a = Turn(mine: false, text: said.joined(separator: " "), sentences: said)
        let hearing = LeaCaptionFeed.exchange(turns: [q, a], live: "", heard: 1, speaking: a.id)
        #expect(hearing.question == "the X button and the B button are here.")
        #expect(hearing.sentence == said[1])
        // Finished: her last sentence.
        #expect(LeaCaptionFeed.exchange(turns: [q, a], live: "", heard: nil, speaking: nil).sentence == said.last)
        // An index past what has arrived shows the newest, never nothing.
        #expect(LeaCaptionFeed.exchange(turns: [q, a], live: "", heard: 99, speaking: a.id).sentence == said.last)
    }

    @Test func aNewQuestionClearsTheOldAnswerAndLiveWordsWin() {
        let said = LeaText.sentences(Self.whereX)
        let q1 = Turn(mine: true, text: "where's the X button")
        let a1 = Turn(mine: false, text: said.joined(separator: " "), sentences: said)
        let q2 = Turn(mine: true, text: "where's the B button")
        let next = LeaCaptionFeed.exchange(turns: [q1, a1, q2], live: "", heard: nil, speaking: nil)
        #expect(next.question == "where's the B button")
        #expect(next.sentence == nil)   // the old answer is not this exchange
        let live = LeaCaptionFeed.exchange(turns: [q1, a1], live: "  where's the  ", heard: nil, speaking: nil)
        #expect(live.question == "where's the")
        #expect(live.sentence == nil)
    }

    @Test func emptyTurnsAreNeverTheExchange() {
        let said = LeaText.sentences(Self.whereX)
        let q = Turn(mine: true, text: "where's the X button")
        let a = Turn(mine: false, text: said.joined(separator: " "), sentences: said)
        let blank = Turn(mine: true, text: "  ")
        #expect(LeaCaptionFeed.exchange(turns: [q, a, blank], live: "", heard: nil, speaking: nil).sentence == said.last)
    }

    @Test func heardIndexFollowsTheAudioClock() {
        let starts = [0.0, 3.2, 6.9]
        #expect(LeaConversation.heardIndex(starts: starts, played: 0) == 0)
        #expect(LeaConversation.heardIndex(starts: starts, played: 3.1) == 0)
        #expect(LeaConversation.heardIndex(starts: starts, played: 3.18) == 1)   // 50 ms early is on time
        #expect(LeaConversation.heardIndex(starts: starts, played: 30) == 2)
        #expect(LeaConversation.heardIndex(starts: [], played: 5) == 0)
    }
}

/// The same words heard twice (Apple's final and Ink-2's end for one utterance) are one turn, not a second question
/// that cancels and replaces the answer.
@MainActor
struct LeaConversationTurnTests {
    @Test func theSameWordsHeardTwiceAreOneTurn() {
        let convo = LeaConversation(scanID: nil, gateway: GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil), diag: nil)
        convo.heard("where's the X button")
        convo.heard("Where's the X button?")
        #expect(convo.turns.filter(\.mine).map(\.text) == ["where's the X button"])
        convo.stopLea()
        #expect(convo.phase == .idle)
        #expect(convo.talkState == .idle)
    }

    /// A stopped answer never moves the 3D model afterwards (Greptile P2): the move waiting for its sentence is
    /// cancelled by stopLea, and one left alone still lands (so the wait was real).
    @Test func stoppingATurnCancelsItsPendingMoves() async throws {
        let gateway = GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil)
        let events: [ConverseEvent] = [.sentence("The X button is the blue one with the letter X.", audio: false),
                                       .stage(.show("ix_3"))]   // a real turn's first sentence and move (xbox_where_x)
        let stopped = LeaConversation(scanID: nil, gateway: gateway, diag: nil)
        var movedAfterStop: [StageCommand] = []
        stopped.onStage = { movedAfterStop.append($0) }
        stopped.applyForTesting(events)
        #expect(stopped.pendingStageMovesForTesting == 1)
        stopped.stopLea()
        #expect(stopped.pendingStageMovesForTesting == 0)

        let kept = LeaConversation(scanID: nil, gateway: gateway, diag: nil)
        var moved: [StageCommand] = []
        kept.onStage = { moved.append($0) }
        kept.applyForTesting(events)
        try await Task.sleep(for: .seconds(1))
        #expect(movedAfterStop.isEmpty)
        #expect(moved.count == 1)
        kept.stopLea()
    }

    @Test func leaSaysNeverShowsMarkersAndNeverOpensTheMicByItself() {
        let convo = LeaConversation(scanID: nil, gateway: GatewayClient(baseURL: URL(string: "http://127.0.0.1:9")!, token: nil), diag: nil)
        convo.leaSays("<show part=\"ix_3\"/>")
        #expect(convo.turns.isEmpty)   // nothing to show: no empty bubble
        #expect(!convo.recording)
    }
}
