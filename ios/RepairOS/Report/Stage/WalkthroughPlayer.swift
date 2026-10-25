import Foundation
import Observation

/// Plays a walkthrough (Walkthrough.steps): Lea says each step's line, then a short beat, then the pager glides to
/// the next card and the camera with it. Swiping to a card narrates that card instead; pause stops her mid-line and
/// resume says the card again from its start. When she is muted (or the phone's voice is off) each card stays up
/// for about as long as reading it takes, so the tour still moves on.
///
/// The person talking pauses the tour (the conversation answers them); "stop" ends it (Walkthrough.isStop, checked
/// by WakeListener and LeaConversation through `current`). Anything else cutting Lea off (hold to talk, another
/// line) pauses it too: her line's utterance id changed before it finished.
@MainActor
@Observable
final class WalkthroughPlayer {
    /// The tour on screen, for the voice paths ("stop", the person starting to talk).
    static weak var current: WalkthroughPlayer?

    private(set) var steps: [Walkthrough.Step] = []
    /// The card being narrated (the pager scrolls to it; a swipe that settles sets it through `go(to:)`).
    private(set) var index = 0
    /// The pager's continuous position (1.5: halfway between the second and third cards). Only the hero reads it,
    /// so a swipe redraws the 3D view and nothing else.
    var progress: Double = 0
    private(set) var active = false
    private(set) var playing = false
    /// The last card has been said: the play control replays from the start.
    private(set) var finished = false

    @ObservationIgnored private var task: Task<Void, Never>?
    /// The first card waits for Lea to finish what she is saying (the voice answer that asked for the tour).
    @ObservationIgnored private var waitForLea = false

    /// The camera arrives before Lea starts talking about the spot.
    static let lead: Duration = .milliseconds(450)
    /// The pause after each line before the next card.
    static let beat: Duration = .milliseconds(700)

    var frame: TourFrame { TourFrame(focuses: steps.map(\.focus), progress: progress) }
    var step: Walkthrough.Step? { steps.indices.contains(index) ? steps[index] : nil }

    // MARK: Controls

    func start(_ steps: [Walkthrough.Step], afterLea: Bool = false) {
        guard !steps.isEmpty else { return }
        self.steps = steps
        index = 0
        progress = 0
        active = true
        finished = false
        waitForLea = afterLea
        Self.current = self
        play()
    }

    func play() {
        guard active else { return }
        if finished { finished = false; index = 0 }
        playing = true
        narrate()
    }

    func pause() {
        guard playing else { return }
        playing = false
        task?.cancel()
        LeaVoice.shared.stop()
    }

    func toggle() { playing ? pause() : play() }

    func next() { go(to: index + 1) }
    func back() { go(to: index - 1) }

    /// The pager settled on card `i` (a swipe, back or next): it narrates that card when the tour is playing. Settling
    /// on the card already shown does nothing, so the tour stops at its last card and never says it again by itself.
    func go(to i: Int) {
        guard active, steps.indices.contains(i), i != index else { return }
        index = i
        finished = false
        if playing { narrate() } else { LeaVoice.shared.stop() }
    }

    func stop() {
        guard active else { return }
        task?.cancel()
        if playing { LeaVoice.shared.stop() }
        active = false
        playing = false
        finished = false
        if Self.current === self { Self.current = nil }
    }

    // MARK: Voice

    /// The person started talking: Lea stops and the tour waits (the conversation answers them).
    func personSpoke() { pause() }

    /// What the person said: true when it ended the tour ("stop"), so the conversation does not send it on.
    func heard(_ words: String, leaSpeaking: Bool) -> Bool {
        guard active, Walkthrough.isStop(words, leaSpeaking: leaSpeaking) else { return false }
        stop()
        return true
    }

    // MARK: Playback

    private func narrate() {
        task?.cancel()
        guard active, playing, let step else { return }
        let i = index
        let wait = waitForLea
        waitForLea = false
        task = Task { [weak self] in
            let voice = LeaVoice.shared
            if wait {
                let until = ContinuousClock.now + .seconds(12)
                while voice.speaking, ContinuousClock.now < until, !Task.isCancelled { try? await Task.sleep(for: .milliseconds(100)) }
            }
            try? await Task.sleep(for: Self.lead)
            guard !Task.isCancelled else { return }
            if !voice.muted {
                voice.say(step.speech)
                let id = voice.utteranceID
                // Never stuck on a line whose end is not reported: at most twice a slow reading of it, plus 10 s.
                let until = ContinuousClock.now + .seconds(Double(step.speech.count) / 6 + 10)
                while voice.speaking, voice.utteranceID == id, !Task.isCancelled, ContinuousClock.now < until {
                    try? await Task.sleep(for: .milliseconds(80))
                }
                guard !Task.isCancelled else { return }
                if voice.utteranceID != id, !voice.muted {
                    self?.pause()   // something else cut her off: wait for the person
                    return
                }
            }
            if voice.muted { try? await Task.sleep(for: Self.readingTime(step.speech)) }
            try? await Task.sleep(for: Self.beat)
            guard !Task.isCancelled, let self, self.active, self.playing, self.index == i else { return }
            if i + 1 < self.steps.count {
                self.index = i + 1
                self.narrate()
            } else {
                self.playing = false
                self.finished = true
            }
        }
    }

    /// How long a card stays up when nothing is said: about 15 characters a second, 2.5 to 9 s.
    nonisolated static func readingTime(_ text: String) -> Duration {
        .milliseconds(Int(min(9, max(2.5, Double(text.count) / 15)) * 1000))
    }
}
