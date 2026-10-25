import Foundation

/// The talk button (the owner, 2026-10-25: "press and release and can talk, press again to stop, and it won't start
/// again until you press again"). A strict tap toggle, kept apart from the views so every transition is tested:
///
///     idle ──tap──▶ listening ──tap──▶ sending ──answer──▶ speaking ──done──▶ idle
///                      ▲                  │                    │
///                      └──────tap─────────┴────────tap─────────┘   (a tap stops Lea and listens: barge-in by tap)
///
/// A second tap inside `debounce` is ignored, so a fast double tap never toggles twice. Nothing here reopens the mic
/// on its own: after an answer the state is idle until the next tap.
struct TalkToggle: Equatable, Sendable {
    enum State: Equatable, Sendable { case idle, listening, sending, speaking }

    /// What the conversation does for a tap.
    enum Effect: Equatable, Sendable {
        /// A tap inside the debounce window: nothing happens.
        case ignored
        /// Idle: start recording.
        case startListening
        /// Listening: stop recording and send what was said.
        case stopAndSend
        /// Sending or speaking: drop the answer (Lea stops at once) and start recording.
        case interruptAndListen
    }

    /// Taps closer together than this are one tap.
    static let debounce: TimeInterval = 0.3

    private(set) var state: State = .idle
    private(set) var lastTap: Date?

    init(state: State = .idle) { self.state = state }

    mutating func tap(at now: Date) -> Effect {
        if let lastTap, now >= lastTap, now.timeIntervalSince(lastTap) < Self.debounce { return .ignored }
        lastTap = now
        switch state {
        case .idle:
            state = .listening
            return .startListening
        case .listening:
            state = .sending
            return .stopAndSend
        case .sending, .speaking:
            state = .listening
            return .interruptAndListen
        }
    }

    // The conversation moving on its own.

    /// Lea's first sentence of the answer arrived.
    mutating func answerStarted() { if state == .sending { state = .speaking } }
    /// The answer finished, failed, or was dropped (also: the recording held no words).
    mutating func answerEnded() { if state == .sending || state == .speaking { state = .idle } }
    /// The microphone could not start, or the recording was too short to send.
    mutating func listeningEnded() { if state == .listening { state = .idle } }

    /// Puts the machine in step with the conversation, which owns the truth (a wake-word turn, a typed turn, an
    /// answer ending while nobody tapped). The debounce clock is kept.
    mutating func sync(_ s: State) { state = s }
}
