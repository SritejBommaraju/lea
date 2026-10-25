import Foundation
import Testing
@testable import RepairOS

/// The talk button as a strict tap toggle (Voice/TalkToggle; the owner, 2026-10-25): tap to talk, tap to send, a tap
/// while Lea answers stops her and listens, double taps are one tap, and nothing reopens the mic on its own.
struct TalkToggleTests {
    let t0 = Date(timeIntervalSinceReferenceDate: 1_000)
    func at(_ s: Double) -> Date { t0.addingTimeInterval(s) }

    @Test func fullTurnIdleListeningSendingSpeakingIdle() {
        var m = TalkToggle()
        #expect(m.state == .idle)
        #expect(m.tap(at: at(0)) == .startListening)
        #expect(m.state == .listening)
        #expect(m.tap(at: at(2)) == .stopAndSend)
        #expect(m.state == .sending)
        m.answerStarted()
        #expect(m.state == .speaking)
        m.answerEnded()
        #expect(m.state == .idle)   // the mic stays off until the next tap
    }

    @Test func aTapWhileSpeakingStopsHerAndListens() {
        var m = TalkToggle(state: .speaking)
        #expect(m.tap(at: at(0)) == .interruptAndListen)
        #expect(m.state == .listening)
        // Her answer ending now (the stopped stream) does not end the new recording.
        m.answerEnded()
        #expect(m.state == .listening)
        #expect(m.tap(at: at(1)) == .stopAndSend)
    }

    @Test func aTapWhileSendingDropsTheAnswerAndListens() {
        var m = TalkToggle()
        _ = m.tap(at: at(0))
        _ = m.tap(at: at(1.5))
        #expect(m.state == .sending)
        #expect(m.tap(at: at(2)) == .interruptAndListen)
        #expect(m.state == .listening)
        m.answerStarted()   // a late sentence of the dropped answer changes nothing
        #expect(m.state == .listening)
    }

    @Test func aDoubleTapIsOneTap() {
        var m = TalkToggle()
        #expect(m.tap(at: at(0)) == .startListening)
        #expect(m.tap(at: at(0.12)) == .ignored)
        #expect(m.state == .listening)   // not toggled straight back off
        #expect(m.tap(at: at(0.29)) == .ignored)
        #expect(m.tap(at: at(0.31)) == .stopAndSend)
    }

    @Test func aFastTapTapTapNeverSticks() {
        var m = TalkToggle()
        var effects: [TalkToggle.Effect] = []
        for t in [0, 0.05, 0.1, 0.35, 0.4, 0.45, 0.7, 0.75, 1.05, 1.1] { effects.append(m.tap(at: at(t))) }
        // Only taps at least 0.3 s after the last one that counted: on, off, on (barge-in), off.
        #expect(effects.filter { $0 != .ignored } == [.startListening, .stopAndSend, .interruptAndListen, .stopAndSend])
        #expect(m.state == .sending)
        m.answerEnded()
        #expect(m.state == .idle)
        #expect(m.tap(at: at(5)) == .startListening)
    }

    @Test func aRecordingThatComesToNothingGoesBackToIdle() {
        var m = TalkToggle()
        _ = m.tap(at: at(0))
        m.listeningEnded()   // the mic refused, or the clip was too short to hold a word
        #expect(m.state == .idle)
        #expect(m.tap(at: at(1)) == .startListening)
    }

    @Test func syncFollowsTheConversation() {
        // A "Hey Lea" turn is answering: a tap barges in, as on a tapped turn.
        var m = TalkToggle()
        m.sync(.speaking)
        #expect(m.tap(at: at(0)) == .interruptAndListen)
        // The debounce clock survives a sync.
        m.sync(.idle)
        #expect(m.tap(at: at(0.1)) == .ignored)
    }

    @Test func aClockGoingBackwardsIsNotADoubleTap() {
        var m = TalkToggle()
        _ = m.tap(at: at(10))
        #expect(m.tap(at: at(9)) == .stopAndSend)
    }
}

/// Home's selection mode and the batched delete (App/ScansView: ScanSelection, ScanLibrary.delete(_:)).
@MainActor
struct ScanSelectionTests {
    @Test func selectToggleAllAndEnd() {
        var s = ScanSelection()
        let all: Set<String> = ["a", "b", "c"]
        s.toggle("a", selectable: all)
        #expect(s.count == 0)   // not in selection mode: nothing ticks
        s.begin(selectable: all)
        #expect(s.active && s.count == 0)
        s.toggle("a", selectable: all)
        s.toggle("b", selectable: all)
        #expect(s.ids == ["a", "b"])
        s.toggle("a", selectable: all)
        #expect(s.ids == ["b"])
        #expect(!s.allSelected(all))
        s.toggleAll(all)
        #expect(s.allSelected(all) && s.count == 3)
        s.toggleAll(all)   // Deselect all
        #expect(s.count == 0 && s.active)
        s.end()
        #expect(!s.active && s.count == 0)
    }

    @Test func theScanBeingCapturedCannotBeTicked() {
        var s = ScanSelection()
        let deletable: Set<String> = ["a", "c"]   // "b" is being captured
        s.begin(with: "b", selectable: deletable)
        #expect(s.active && s.count == 0)
        s.toggle("b", selectable: deletable)
        #expect(!s.contains("b"))
        s.toggleAll(deletable)
        #expect(s.ids == ["a", "c"])
        #expect(s.allSelected(deletable))
    }

    @Test func pruneDropsScansThatAreGone() {
        var s = ScanSelection()
        s.begin(selectable: ["a", "b", "c"])
        s.toggleAll(["a", "b", "c"])
        s.prune(to: ["c"])
        #expect(s.ids == ["c"])
        #expect(!s.allSelected([]))   // an empty list is never "all selected"
    }

    @Test func titles() {
        #expect(ScanSelection.deleteTitle(1) == "Delete 1 scan")
        #expect(ScanSelection.deleteTitle(4) == "Delete 4 scans")
        #expect(ScanSelection.confirmTitle(4) == "Delete 4 scans?")
        #expect(ScanSelection.confirmMessage == "Photos and reports are removed from this phone. Copies on the server stay.")
    }

    func storage() -> ScanStorage {
        ScanStorage(root: FileManager.default.temporaryDirectory.appending(path: "repairos-tests-\(UUID().uuidString)"))
    }

    @Test func batchDeleteSkipsTheCaptureDeletesEachOnceAndReloadsOnce() throws {
        let st = storage()
        let a = try st.newSession(symptom: nil, engine: .arkit)
        let b = try st.newSession(symptom: nil, engine: .arkit)
        let c = try st.newSession(symptom: nil, engine: .arkit)
        CaptureRegistry.active = b.id
        defer { CaptureRegistry.active = nil }
        var reloads = 0
        let n = ScanLibrary.delete([a, b, c, a], storage: st) { reloads += 1 }   // a duplicate id, and the capture
        #expect(n == 2)
        #expect(reloads == 1)
        #expect(st.listSessions().map(\.id) == [b.id])
        #expect(!FileManager.default.fileExists(atPath: st.dir(a).path))
        #expect(!FileManager.default.fileExists(atPath: st.dir(c).path))
    }

    @Test func deletingEverythingLeavesAnEmptyList() throws {
        let st = storage()
        let all = try (0..<3).map { _ in try st.newSession(symptom: nil, engine: .arkit) }
        var reloads = 0
        #expect(ScanLibrary.delete(all, storage: st) { reloads += 1 } == 3)
        #expect(st.listSessions().isEmpty)
        #expect(reloads == 1)
        // Nothing left to delete: still one reload, no crash.
        #expect(ScanLibrary.delete(all, storage: st) { reloads += 1 } == 0)
        #expect(reloads == 2)
    }
}
