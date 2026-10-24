import Foundation
import Observation
import UIKit

/// A conversation with Lea about one scan (or, on the home screen, about what is wrong before a scan). Each turn
/// goes to POST /v1/voice/converse: the recording (hold to talk, Cartesia's recognizer) or the words the phone
/// already heard ("Hey Lea" and hands-free follow-ups, Apple's on-device recognizer). Lea's answer streams back
/// sentence by sentence and is played as it arrives; captions show both sides.
@MainActor
@Observable
final class LeaConversation {
    struct Turn: Identifiable, Equatable {
        let id = UUID()
        var mine: Bool
        var text: String
    }

    enum Phase: Equatable { case idle, listening, thinking, speaking, failed(String) }

    /// The conversation on screen now (the report's, or the home sheet's); "Hey Lea" talks to it. Counted by
    /// appearances, so a panel SwiftUI re-mounts (appear before the old disappear) stays the active one.
    static var active: LeaConversation? {
        registry.removeAll { $0.value == nil }
        return registry.last { ($0.value?.visible ?? 0) > 0 }?.value
    }
    private struct Weak { weak var value: LeaConversation? }
    private static var registry: [Weak] = []
    @ObservationIgnored private(set) var visible = 0

    func appeared() {
        visible += 1
        Self.registry.removeAll { $0.value === self || $0.value == nil }
        Self.registry.append(Weak(value: self))
    }

    func disappeared() { visible = max(0, visible - 1) }

    let scanID: String?
    let gateway: GatewayClient
    let diag: Diag?
    /// The gateway session, for sending what the person suspects (`POST /v1/sessions/{id}/story`).
    var sessionID: () -> String? = { nil }

    private(set) var turns: [Turn] = []
    private(set) var phase: Phase = .idle
    /// What the person is saying right now (live, from the phone's recognizer).
    private(set) var liveCaption = ""
    private(set) var action: (kind: String, label: String)?
    /// Hands-free: after Lea answers, listen for the next turn without the wake word.
    var handsFree = true
    /// Called after each of Lea's answers (the home sheet: "Let me look that up…" while the brief is coming).
    @ObservationIgnored var afterTurn: (@MainActor () -> Void)?

    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private let recorder = VoiceRecorder()
    @ObservationIgnored private var loaded = false
    @ObservationIgnored private var firstAudioMs: Int?

    init(scanID: String?, gateway: GatewayClient, diag: Diag?) {
        self.scanID = scanID
        self.gateway = gateway
        self.diag = diag
    }

    var actionKind: String? { action?.kind }

    func loadHistory() async {
        guard !loaded else { return }
        loaded = true
        let past = (try? await gateway.conversation(scanID: scanID)) ?? []
        if turns.isEmpty { turns = past.map { Turn(mine: $0.role == "user", text: $0.text) } }
    }

    // MARK: Hold to talk

    func pressToTalk() {
        stopLea()
        WakeListener.shared.suspend(true)
        phase = .listening
        Task {
            let ok = await recorder.start()
            if phase != .listening { recorder.cancel(); return }   // a tap, already let go
            if !ok { phase = .failed("Lea can't hear you: allow the microphone in Settings.") }
        }
    }

    /// A tap, not a hold: drop the recording (hands-free listening takes over).
    func cancelHold() {
        recorder.cancel()
        WakeListener.shared.suspend(false)
        if phase == .listening { phase = .idle }
    }

    func releaseToTalk() {
        guard phase == .listening else { return }
        let audio = recorder.stop()
        WakeListener.shared.suspend(false)
        guard let audio else { phase = .idle; return }
        let released = Date()
        if LeaVoice.shared.status?.cartesia == true {
            send(audio: audio, text: nil, since: released)
        } else {
            phase = .thinking
            Task {
                let words = await recorder.transcribeOnDevice()
                guard !words.isEmpty else { phase = .idle; return }
                send(audio: nil, text: words, since: released)
            }
        }
    }

    // MARK: Words the phone heard (Hey Lea, hands-free)

    func heard(_ text: String) {
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        liveCaption = ""
        guard !words.isEmpty else { return }
        send(audio: nil, text: words, since: Date())
    }

    /// Lea says something on her own (what the object is, once the backend works it out).
    func leaSays(_ text: String) {
        turns.append(Turn(mine: false, text: text))
        LeaVoice.shared.say(text)
        // Then listen for the reply, hands-free, once she has finished.
        Task {
            while LeaVoice.shared.speaking { try? await Task.sleep(for: .milliseconds(200)) }
            if handsFree, phase == .idle { WakeListener.shared.listenForFollowUp() }
        }
    }

    func showLive(_ caption: String) {
        stopLea()   // the person is talking: Lea stops at once
        liveCaption = caption
        phase = .listening
    }

    /// Lea stops talking now (the person started to speak, or pressed the button).
    func stopLea() {
        task?.cancel()
        LeaVoice.shared.stop()
        if phase == .speaking || phase == .thinking { phase = .idle }
    }

    // MARK: One turn

    private func send(audio: Data?, text: String?, since start: Date) {
        task?.cancel()
        action = nil
        phase = .thinking
        if let text { turns.append(Turn(mine: true, text: text)); postStory(text) }
        let speech = LeaVoice.shared.status?.cartesia == true && !LeaVoice.shared.muted
        let stream = gateway.converse(scanID: scanID, audio: audio, text: text, speech: speech)
        let voice = LeaVoice.shared
        let id = voice.beginStream()
        firstAudioMs = nil
        var firstSentence: Int?
        task = Task {
            var answer = Turn(mine: false, text: "")
            var answerIndex: Int?
            var unvoiced: String?   // marked for Cartesia, no audio yet: the phone says it if Cartesia gives up
            do {
                for try await event in stream {
                    if Task.isCancelled { break }
                    switch event {
                    case .transcript(let t):
                        if audio != nil, !t.isEmpty { turns.append(Turn(mine: true, text: t)); postStory(t) }
                    case .sentence(let s, let withAudio):
                        firstSentence = firstSentence ?? Self.ms(since: start)
                        answer.text = answer.text.isEmpty ? s : answer.text + " " + s
                        if let i = answerIndex { turns[i] = answer } else { turns.append(answer); answerIndex = turns.count - 1 }
                        phase = .speaking
                        if !withAudio { voice.queueOnDevice(s, stream: id) }
                        unvoiced = withAudio ? s : nil
                    case .audio(let pcm):
                        unvoiced = nil
                        voice.appendStream(pcm, stream: id) { [weak self] in
                            let ms = Self.ms(since: start)
                            Task { @MainActor in if self?.firstAudioMs == nil { self?.firstAudioMs = ms } }
                        }
                    case .audioOff:
                        // Cartesia refused this sentence before any audio; the rest come with audio: false.
                        if let s = unvoiced { voice.queueOnDevice(s, stream: id) }
                        unvoiced = nil
                    case .action(let kind, let label):
                        action = (kind, label)
                    case .done(_, let timings):
                        diag?.log("voice_turn", ["input": audio == nil ? "phone_words" : "recording",
                                                 "end_to_first_sentence_ms": firstSentence ?? -1,
                                                 "end_to_first_audio_ms": firstAudioMs ?? -1, "server_ms": timings])
                    }
                }
                await voice.endStream(id)
                if !Task.isCancelled {
                    phase = .idle
                    afterTurn?()
                    if handsFree, phase == .idle { WakeListener.shared.listenForFollowUp() }
                }
            } catch {
                voice.endStreamNow(id)
                phase = .failed(AskLeaBar.message(error))
            }
        }
    }

    /// What the person says is also their guess about what is wrong: the gateway turns it into a lead (404 while
    /// that route does not exist is ignored).
    private func postStory(_ text: String) {
        guard let sid = sessionID() else { return }
        let gateway = self.gateway, scan = scanID
        Task.detached { try? await gateway.postStory(sessionID: sid, text: text, scanID: scan) }
    }

    nonisolated private static func ms(since d: Date) -> Int { Int(Date().timeIntervalSince(d) * 1000) }
}

extension GatewayClient {
    /// POST /v1/sessions/{id}/story {text}. A 404 (the route is not there yet) is not an error.
    func postStory(sessionID: String, text: String, scanID: String? = nil) async throws {
        var req = voiceRequest("POST", "v1/sessions/\(sessionID)/story")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["text": String(text.prefix(4000)), "source": "voice"]
        if let scanID { body["scan_id"] = scanID }
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        req.timeoutInterval = 10
        _ = try? await URLSession.shared.data(for: req)
    }
}
