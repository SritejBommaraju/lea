import Foundation
import Observation
import UIKit

/// A conversation with Lea about one scan (or, on the home screen, about what is wrong before a scan). Each turn
/// goes to POST /v1/voice/converse: the recording (the talk button, Cartesia's recognizer) or the words the phone
/// already heard ("Hey Lea" and hands-free follow-ups, Apple's on-device recognizer). Lea's answer streams back
/// sentence by sentence and is played as it arrives; captions show both sides, Lea's in step with her voice
/// (`heardSentence`). The talk button is a strict tap toggle (`TalkToggle`, `tapTalk`): the mic opens on a tap and
/// stays off after the answer until the next tap.
@MainActor
@Observable
final class LeaConversation {
    struct Turn: Identifiable, Equatable {
        let id = UUID()
        var mine: Bool
        var text: String
        /// Lea's answer as the sentences she said, in order (the caption strip shows one; `text` is them joined).
        var sentences: [String] = []
        /// The real photos (crop blob shas, ringed) of the spots Lea showed in this answer.
        var photos: [String] = []
        /// Which part each photo shows (the `photo` event's `for`: a finding or part id), for the stage's hand-off.
        var photoFor: [String: String] = [:]
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

    func disappeared() {
        visible = max(0, visible - 1)
        // The last conversation left the screen: hands-free follow-up ends with it ("Hey Lea" opens one again).
        if visible == 0, Self.active == nil { WakeListener.shared.endFollowUp() }
    }

    let scanID: String?
    let gateway: GatewayClient
    let diag: Diag?
    /// The gateway session, for sending what the person suspects (`POST /v1/sessions/{id}/story`).
    var sessionID: () -> String? = { nil }
    /// What the screen shows now, sent with each turn (a scan's stage, cue and progress); empty off the scanner.
    @ObservationIgnored var live: () -> [String: String] = { [:] }
    /// While scanning: Lea's eyes (Voice/LeaSight). A turn's photos are taken when it starts and uploaded while the
    /// person talks (plus the keyframes of the last few seconds); her marks and measures land on the object. Nil off
    /// the scanner: turns go on words alone.
    @ObservationIgnored var eyes: LeaEyes?
    /// Lea offered an action ("Start a scan") and the person said yes out loud: the same as tapping it.
    @ObservationIgnored var acceptOffer: ((String) -> Void)?
    /// On a report: Lea moves its 3D model (LeaStage), each move timed to land with the sentence it belongs to.
    @ObservationIgnored var onStage: ((StageCommand) -> Void)?

    private(set) var turns: [Turn] = []
    /// How many of `turns` came from the saved history (an earlier visit): the caption strip shows only this visit.
    private(set) var historyCount = 0
    private(set) var phase: Phase = .idle
    /// The talk button's microphone is recording (tap to start, tap again to send).
    private(set) var recording = false
    /// Which sentence of the answer being spoken the person is hearing now (an index into its `sentences`), so the
    /// captions follow her voice instead of running ahead of it; nil when no answer is playing.
    private(set) var heardSentence: Int?
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
    @ObservationIgnored private var current: TurnState?
    /// 3D moves waiting for their sentence to be heard: cancelled when Lea is stopped or a new turn starts, so a
    /// stopped answer never moves the model afterwards.
    @ObservationIgnored private var stageMoves: [Task<Void, Never>] = []
    /// An answer started early on Ink-2's eager end: held until confirmed.
    @ObservationIgnored private var holding = false
    @ObservationIgnored private var heldEvents: [ConverseEvent] = []
    /// A confirmed early answer's commit still in flight: every next turn waits for it, so its answer sees that exchange.
    @ObservationIgnored private var pendingCommit: Task<Void, Never>?
    /// A confirmed early answer not known to be saved (its commit failed or is still out): the next turn carries its id
    /// (`converse?commit=`), and the gateway saves it before answering.
    @ObservationIgnored private var unsavedHeld: String?
    @ObservationIgnored private var speculativeText: String?
    /// The offer on screen when an early (Ink-2 eager) answer started: kept until the final words decide, so a
    /// final "yes" can still take it, and put back if the early answer is dropped.
    @ObservationIgnored private var heldOffer: (kind: String, label: String)?
    @ObservationIgnored private var speculativeTurn: UUID?
    /// The talk button (tap toggle with a debounce).
    @ObservationIgnored private var talk = TalkToggle()
    /// The recording starting (serialised: a fast tap-tap-tap never leaves two starts racing for one recorder).
    @ObservationIgnored private var recordStart: Task<Void, Never>?
    @ObservationIgnored private var recordGeneration = 0
    /// The last words sent as a turn and when: the same words heard again a moment later (Apple's final and Ink-2's
    /// end for one utterance) are one turn, never a second question that replaces the answer.
    @ObservationIgnored private var lastSent: (key: String, at: Date)?
    /// Keeps `heardSentence` in step with the audio while an answer plays.
    @ObservationIgnored private var captionClock: Task<Void, Never>?

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
        guard turns.isEmpty else { return }
        turns = past.compactMap { t in
            let mine = t.role == "user"
            let sentences = mine ? [] : LeaText.sentences(t.text)
            let text = mine ? t.text.trimmingCharacters(in: .whitespacesAndNewlines) : sentences.joined(separator: " ")
            return text.isEmpty ? nil : Turn(mine: mine, text: text, sentences: sentences)
        }
        historyCount = turns.count
    }

    // MARK: The talk button (tap to talk, tap to send)

    /// What the talk button is doing, from the conversation's own state.
    var talkState: TalkToggle.State {
        if recording { return .listening }
        switch phase {
        case .thinking: return .sending
        case .speaking: return .speaking
        default: return .idle
        }
    }

    /// A tap on the talk button: idle starts listening, listening stops and sends, and while Lea is answering it stops
    /// her and listens (barge-in by tap). Taps within `TalkToggle.debounce` of the last one are ignored.
    func tapTalk(at now: Date = Date()) {
        talk.sync(talkState)
        switch talk.tap(at: now) {
        case .ignored: return
        case .startListening, .interruptAndListen: startRecording()
        case .stopAndSend: stopRecordingAndSend()
        }
    }

    private func startRecording() {
        personStarted()   // Lea stops at once; a walkthrough waits
        cancelSpeculation()
        // The button owns this turn: whatever the wake listener was hearing is dropped, and no follow-up window stays open.
        WakeListener.shared.endFollowUp()
        WakeListener.shared.dropPendingTurn()
        WakeListener.shared.suspend(true)
        liveCaption = ""
        recording = true
        phase = .listening
        recordGeneration += 1
        let generation = recordGeneration, previous = recordStart
        recordStart = Task {
            await previous?.value
            guard recording, generation == recordGeneration else { return }   // already stopped: never start late
            let ok = await recorder.start()
            guard recording, generation == recordGeneration else { if ok { recorder.cancel() }; return }
            if !ok {
                recording = false
                talk.listeningEnded()
                WakeListener.shared.suspend(false)
                phase = .failed("Lea can't hear you: allow the microphone in Settings.")
            }
        }
    }

    /// Stops the recording and sends it (or the phone's transcript of it). Too short to hold a word: back to idle.
    private func stopRecordingAndSend() {
        guard recording else { return }
        recording = false
        recordGeneration += 1
        let audio = recorder.stop()
        WakeListener.shared.suspend(false)
        guard let audio else {
            talk.listeningEnded()
            phase = .idle
            return
        }
        let released = Date()
        if LeaVoice.shared.status?.cartesia == true {
            send(audio: audio, text: nil, since: released, followUp: false)
        } else {
            phase = .thinking
            task?.cancel()
            task = Task {
                let words = await recorder.transcribeOnDevice()
                guard !Task.isCancelled, phase == .thinking else { return }   // a tap took over meanwhile
                guard !words.isEmpty else { phase = .idle; return }
                if accept(words) { return }   // said yes to the offer on screen
                send(audio: nil, text: words, since: released, input: "recording_on_device", followUp: false)
            }
        }
    }

    /// The recording ends without being sent (typing took over, the screen went away).
    func cancelRecording() {
        guard recording else { return }
        recording = false
        recordGeneration += 1
        recorder.cancel()
        talk.listeningEnded()
        WakeListener.shared.suspend(false)
        if phase == .listening { phase = .idle }
    }

    // MARK: Words the phone heard (Hey Lea, hands-free)

    /// The person's turn is over (Apple's recognizer after a pause, or Ink-2's `turn.end`). If an answer was already
    /// started on Ink-2's `turn.eager_end` for the same words, it is released now instead of asked again.
    func heard(_ text: String, engine: String = "apple") {
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        liveCaption = ""
        // "Stop" during a walkthrough ends it; it is not a question for Lea.
        if let tour = WalkthroughPlayer.current, tour.heard(words, leaSpeaking: false) {
            cancelSpeculation()
            turnDropped()
            return
        }
        if speculativeText != nil, (action ?? heldOffer) != nil, acceptOffer != nil, Self.isYes(words) { cancelSpeculation() }
        if let spec = speculativeText {
            if Self.same(spec, words) {
                confirmSpeculation(final: words)
                return
            }
            cancelSpeculation()
        }
        guard !words.isEmpty else { return }
        // The same words again a moment later while she is answering them: one turn, not a second question.
        if answering, let last = lastSent, last.key == LeaText.key(words), Date().timeIntervalSince(last.at) < 4 { return }
        if accept(words) { return }
        // A spoken turn ("Hey Lea", hands-free) may be followed by listening; a typed one never opens the mic.
        send(audio: nil, text: words, since: Date(), input: engine, followUp: engine != "typed")
    }

    /// "Yes" to an offer on screen takes it, without another round trip. True when it did.
    private func accept(_ words: String, offered: String? = nil) -> Bool {
        guard let kind = offered ?? action?.kind, let acceptOffer, Self.isYes(words) else { return false }
        turns.append(Turn(mine: true, text: words))
        action = nil
        phase = .idle
        acceptOffer(kind)
        return true
    }

    /// A plain yes ("yes", "yeah, let's do it", "okay go ahead"), not a question or more detail.
    static func isYes(_ words: String) -> Bool {
        let w = WakeListener.normalize(words)
        let yes = "(yes|yeah|yep|yup|sure|ok|okay|alright|all right|please|go ahead|do it|let's do it|lets do it|let's go|lets go|sounds good|start it|scan it|start the scan)"
        return w.range(of: "^\(yes)( \(yes))*( please| lea| leah| thanks)?$", options: .regularExpression) != nil
    }

    /// Ink-2 thinks the person may be done: start the answer now, but hold it (nothing is shown or played) until
    /// `turn.end` confirms the same words, or drop it on `turn.resume`.
    func speculate(_ text: String) {
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty, speculativeText == nil, phase != .speaking else { return }
        if action != nil, acceptOffer != nil, Self.isYes(words) { return }   // heard() takes the offer
        speculativeText = words
        send(audio: nil, text: words, since: Date(), input: "ink2_eager", hold: true)
    }

    func cancelSpeculation() {
        guard speculativeText != nil else { return }
        speculativeText = nil
        if let offer = heldOffer { action = offer }
        heldOffer = nil
        holding = false
        heldEvents = []
        task?.cancel()
        cancelStageMoves()
        LeaVoice.shared.stop()
        if let id = speculativeTurn { turns.removeAll { $0.id == id } }
        speculativeTurn = nil
        // Still listening only while the person is still being heard (turn.resume); otherwise idle, never a stuck
        // "Listening…" (the hold timed out, the conversation left the screen).
        phase = liveCaption.isEmpty ? .idle : .listening
    }

    private func confirmSpeculation(final words: String) {
        speculativeText = nil
        heldOffer = nil
        if let id = speculativeTurn, let i = turns.firstIndex(where: { $0.id == id }) { turns[i].text = words }
        speculativeTurn = nil
        postStory(words)
        current?.start = Date()   // latency counts from the end of the person's turn
        current?.confirmedEarly = true
        holding = false
        let replay = heldEvents
        heldEvents = []
        if let st = current {
            LeaVoice.shared.streamConfirmed(st.id)
            for e in replay { apply(e, st) }
        }
    }

    private func cancelStageMoves() {
        for move in stageMoves { move.cancel() }
        stageMoves = []
    }

    #if DEBUG
    /// Tests only: applies these events as one turn's stream (no gateway; the stream id matches no playback, so
    /// nothing is heard).
    func applyForTesting(_ events: [ConverseEvent]) {
        let st = TurnState(start: Date(), id: -1, input: "test", recorded: false)
        current = st
        for e in events { apply(e, st) }
    }

    /// Tests only: 3D moves still waiting for their sentence.
    var pendingStageMovesForTesting: Int { stageMoves.filter { !$0.isCancelled }.count }
    #endif

    static func same(_ a: String, _ b: String) -> Bool { WakeListener.normalize(a) == WakeListener.normalize(b) }

    /// A typed turn: Lea stops talking and it goes the way spoken words do ("yes" still takes an offer).
    func typed(_ text: String) {
        // Typing ends anything still being heard: a hold-to-talk recording, or a spoken turn not finished yet (its
        // late words would otherwise cancel this answer and replace it).
        cancelRecording()
        WakeListener.shared.dropPendingTurn()
        liveCaption = ""
        stopLea()
        heard(text, engine: "typed")
    }

    /// Lea says something on her own (what the object is, once the backend works it out). `listenAfter`: then listen
    /// for the reply hands-free (only when the conversation was opened hands-free: "Hey Siri, hey Lea"); otherwise the
    /// mic stays off until the person taps.
    func leaSays(_ text: String, listenAfter: Bool = false) {
        let words = LeaText.clean(text)
        guard !words.isEmpty else { return }
        turns.append(Turn(mine: false, text: words, sentences: LeaText.sentences(words)))
        LeaVoice.shared.say(words)
        guard listenAfter else { return }
        Task {
            while LeaVoice.shared.speaking { try? await Task.sleep(for: .milliseconds(200)) }
            if handsFree, phase == .idle, !recording { WakeListener.shared.listenForFollowUp() }
        }
    }

    func showLive(_ caption: String) {
        personStarted(stopLea: speculativeText == nil)   // the person is talking: Lea stops at once
        liveCaption = caption
        phase = .listening
    }

    /// The person's turn came to nothing for the conversation (it ended the walkthrough): back to idle.
    func turnDropped() {
        liveCaption = ""
        if phase == .listening || phase == .thinking { phase = .idle }
    }

    /// The person started a turn (pressed talk, or the phone heard them): Lea stops and a walkthrough waits for them,
    /// whatever it was doing (mid-line, its lead or beat, a muted card's reading time), so it never talks over them.
    func personStarted(stopLea stop: Bool = true) {
        if stop { stopLea() }
        WalkthroughPlayer.current?.personSpoke()
        turnStarting()
    }

    /// The person's turn is starting (Ink-2's `turn.start`, the first words heard, a tap on the talk button): Lea's eyes take the
    /// camera now and upload it while they talk, so the answer does not wait for a photo.
    func turnStarting() {
        eyes?.turnStarted()
    }

    /// Lea is answering (or about to): the person talking now is a barge-in (WakeListener keeps Ink-2 open for it).
    var answering: Bool { phase == .thinking || phase == .speaking }

    /// Lea stops talking now (the person started to speak, or pressed the button).
    func stopLea() {
        task?.cancel()
        cancelStageMoves()
        LeaVoice.shared.stop()
        // Cut off mid-answer: the transcript keeps what she said, not the sentences she never got to.
        if phase == .speaking, let st = current, let i = st.answerIndex, i < turns.count, let heard = heardSentence,
           heard + 1 < turns[i].sentences.count {
            turns[i].sentences = Array(turns[i].sentences.prefix(heard + 1))
            turns[i].text = turns[i].sentences.joined(separator: " ")
        }
        stopCaptionClock()
        if phase == .speaking || phase == .thinking { phase = .idle }
        talk.answerEnded()
    }

    // MARK: One turn

    /// Per-turn bookkeeping (kept outside the stream loop so a held answer can be replayed on confirmation).
    @MainActor private final class TurnState {
        var start: Date
        /// Gateway-audio path: a sentence whose cloud audio never started is said by the phone (Greptile, PR #13).
        var fallback = StreamFallback()
        let id: Int
        let input: String
        let recorded: Bool
        var answerIndex: Int?
        var answerText = ""
        var firstSentence: Int?
        var confirmedEarly = false
        /// The offer on screen when the turn began ("Start a scan"): a recorded yes takes it.
        var offered: String?
        /// Where Lea pointed in this turn's photo, pinned once the answer is complete.
        var marks: [LeaMark] = []
        /// What the phone measured with LiDAR when Lea asked (said after her answer).
        var measured: String?
        /// When her first sentence arrived (about when it starts being heard), and how many characters come before
        /// the newest sentence: a 3D move is held until its sentence is heard (StageTiming).
        var firstHeardAt: Date?
        var charsBefore = 0
        /// Where the newest sentence starts in this answer's audio (seconds), measured from the player when its text
        /// arrived on the gateway-audio path (the audio of the sentences before it is scheduled by then); nil when
        /// that is unknown (Sonic, the phone's voice), where its start is estimated on the player's clock instead.
        var sentenceAt: Double?
        /// Sonic's socket for this answer (nil: the gateway streams the audio, or the phone's voice).
        var sonic: SonicSocket?
        var sonicFailed = false
        var sonicFinished = false
        /// The sentences handed to Sonic, so a socket that fails part-way can have the unheard ones said by the phone.
        var sonicSent: [String] = []
        /// The confirmed early answer this turn asked the gateway to save first (`unsavedHeld`), if any.
        var carried: String?
        /// Listen for a follow-up without the wake word after this answer (spoken "Hey Lea" turns only; never after a
        /// tap or typing, so the mic stays off until the next tap).
        var followUp = true
        /// Where each shown sentence starts in the answer's audio (seconds; measured, or estimated from the characters
        /// before it), for the captions.
        var sentenceStarts: [Double] = []
        init(start: Date, id: Int, input: String, recorded: Bool) {
            self.start = start; self.id = id; self.input = input; self.recorded = recorded
        }
    }

    private func send(audio: Data?, text: String?, since start: Date, input: String = "apple", hold: Bool = false,
                      followUp: Bool = true) {
        task?.cancel()
        cancelStageMoves()
        stopCaptionClock()
        if let text { lastSent = (LeaText.key(text), Date()) }
        let offered = action?.kind
        if hold { heldOffer = action }
        action = nil
        phase = .thinking
        holding = hold
        heldEvents = []
        if let text {
            let turn = Turn(mine: true, text: text)
            turns.append(turn)
            if hold { speculativeTurn = turn.id } else { postStory(text) }
        }
        let voiceOn = LeaVoice.shared.status?.cartesia == true && !LeaVoice.shared.muted
        // No voice status at all (lost after a gateway change or a failed fetch): ask again for the next turn. This
        // turn falls back to the phone's voice (the owner's cloud turns all went audio=none, 2026-10-25).
        if LeaVoice.shared.status == nil { Task { await LeaVoice.shared.refresh() } }
        // Primary: Sonic's socket on the phone, fed each sentence as it is written (one voice flow; after Samik's
        // PR #11). Without a ready token: the gateway streams the audio in the answer itself.
        let grant = voiceOn && WakeListener.shared.realtimeOn ? RealtimeTokens.ready : nil
        let gateway = self.gateway, scan = scanID, commit = pendingCommit
        let liveNow = live(), session = sessionID(), eyes = self.eyes
        let voice = LeaVoice.shared
        let st = TurnState(start: start, id: voice.beginStream(held: hold), input: audio == nil ? input : "recording",
                           recorded: audio != nil)
        current = st
        st.offered = offered
        st.followUp = followUp
        firstAudioMs = nil
        if let grant { openSonic(grant, st) }
        task = Task {
            defer { st.sonic?.cancel() }
            await commit?.value   // the confirmed exchange first (Greptile P1 on #21); commitHeld gives up after 3 s
            st.carried = self.unsavedHeld
            // Lea looks: the turn's photos were uploaded while the person talked; the question refers to them.
            var liveTurn = liveNow, images: [String] = []
            if let eyes, let seen = await eyes.photos() {
                images = seen.refs
                liveTurn.merge(seen.live) { _, new in new }
            }
            if Task.isCancelled { return }
            let stream = gateway.converse(scanID: scan, audio: audio, text: text, speech: voiceOn && grant == nil, held: hold,
                                          commit: st.carried, live: liveTurn, sessionID: session, images: images)
            do {
                for try await event in stream {
                    if Task.isCancelled { break }
                    if holding { heldEvents.append(event); continue }
                    apply(event, st)
                }
                // A held answer waits for turn.end / turn.resume; if neither comes, it is dropped, never stuck.
                let holdUntil = Date().addingTimeInterval(Self.maxHold)
                while holding, !Task.isCancelled, Date() < holdUntil { try? await Task.sleep(for: .milliseconds(30)) }
                if holding, !Task.isCancelled { cancelSpeculation(); return }   // dropped: back to idle, not stuck
                if Task.isCancelled { return }
                // Sonic may still be sending the last sentence's audio.
                let waitUntil = Date().addingTimeInterval(10)
                while st.sonic != nil, !st.sonicFinished, !st.sonicFailed, Date() < waitUntil, !Task.isCancelled {
                    try? await Task.sleep(for: .milliseconds(40))
                }
                await voice.endStream(st.id)
                if let line = st.measured, !Task.isCancelled {   // the LiDAR number Lea asked for, after her answer
                    turns.append(Turn(mine: false, text: line))
                    voice.say(line)
                    while voice.speaking, !Task.isCancelled { try? await Task.sleep(for: .milliseconds(100)) }
                }
                eyes?.answerEnded()
                if !Task.isCancelled {
                    stopCaptionClock()
                    phase = .idle
                    talk.answerEnded()
                    afterTurn?()
                    if handsFree, st.followUp, phase == .idle, !recording { WakeListener.shared.listenForFollowUp() }
                }
            } catch {
                eyes?.answerEnded()
                if Task.isCancelled { return }
                voice.endStreamNow(st.id)
                stopCaptionClock()
                phase = .failed(AskLeaBar.message(error))
                talk.answerEnded()
            }
        }
    }

    private func apply(_ event: ConverseEvent, _ st: TurnState) {
        let voice = LeaVoice.shared
        switch event {
        case .transcript(let t):
            // Held to talk and said yes to the offer: take it, and drop the gateway's answer.
            if st.recorded, st.offered != nil, accept(t, offered: st.offered) {
                task?.cancel()
                voice.endStreamNow(st.id)
                return
            }
            let words = t.trimmingCharacters(in: .whitespacesAndNewlines)
            if st.recorded, !words.isEmpty {
                turns.append(Turn(mine: true, text: words))
                lastSent = (LeaText.key(words), Date())
                postStory(words)
            }
        case .sentence(let raw, let withAudio):
            st.firstSentence = st.firstSentence ?? Self.ms(since: st.start)
            st.firstHeardAt = st.firstHeardAt ?? Date()
            st.charsBefore = st.answerText.isEmpty ? 0 : st.answerText.count + 1
            st.sentenceAt = st.sonic == nil && withAudio ? voice.scheduledSeconds(stream: st.id) : nil
            st.answerText = st.answerText.isEmpty ? raw : st.answerText + " " + raw
            // What the captions show: cleaned (never a marker or an id), and a sentence she already said in this
            // answer is not shown twice. The audio below still gets the gateway's own sentence.
            let said = st.answerIndex.flatMap { $0 < turns.count ? turns[$0].sentences : nil } ?? []
            let fresh = LeaText.shown(raw, after: said)
            if let s = fresh {
                st.sentenceStarts.append(st.sentenceAt ?? Double(st.charsBefore) / Self.charsPerSecond)
                if let i = st.answerIndex, i < turns.count {
                    turns[i].sentences.append(s)
                    turns[i].text = turns[i].sentences.joined(separator: " ")
                } else {
                    turns.append(Turn(mine: false, text: s, sentences: [s]))
                    st.answerIndex = turns.count - 1
                }
                startCaptionClock(st)
            }
            phase = .speaking
            talk.answerStarted()
            let spoken = fresh ?? ""
            if let sonic = st.sonic, !st.sonicFailed {
                if !spoken.isEmpty { st.sonicSent.append(spoken); sonic.say(spoken + " ", more: true) }
            } else if let say = st.fallback.sentence(spoken, withAudio: withAudio), !say.isEmpty {
                voice.queueOnDevice(say, stream: st.id)
            }
        case .audio(let pcm):
            st.fallback.audio()
            let start = st.start
            voice.appendStream(pcm, stream: st.id) { [weak self] in
                let ms = Self.ms(since: start)
                Task { @MainActor in if self?.firstAudioMs == nil { self?.firstAudioMs = ms } }
            }
        case .audioOff:   // Cartesia stopped before this sentence's audio (gateway path): the phone says it
            if st.sonic == nil || st.sonicFailed, let say = st.fallback.audioOff(), !say.isEmpty { voice.queueOnDevice(say, stream: st.id) }
        case .action(let kind, let label):
            action = (kind, label)
        case .mark(let m):
            st.marks.append(m)
        case .measure(let a, let b):
            st.measured = eyes?.measure(a, b)
        case .stage(let command):
            // The move starts as the sentence that carries it starts playing: on the audio player's own clock (the
            // sentence's measured start, or its estimated start from the characters before it); with no audio
            // (muted, the phone's voice) after 0.4 s at the latest, on the old estimate.
            let target = st.sentenceAt ?? Double(st.charsBefore) / Self.charsPerSecond
            let wait = StageTiming.delay(firstHeardAt: st.firstHeardAt, charsBefore: st.charsBefore, now: Date())
            let id = st.id
            if let played = voice.playedSeconds(stream: id), played + 0.03 >= target { onStage?(command); return }
            stageMoves.append(Task { [weak self] in
                let asked = Date()
                while !Task.isCancelled {
                    guard let self, self.current === st else { return }   // a newer turn took over: its moves win
                    let elapsed = Date().timeIntervalSince(asked)
                    if let played = LeaVoice.shared.playedSeconds(stream: id) {
                        if played + 0.03 >= target || elapsed > 12 { break }
                    } else if elapsed >= max(wait, 0.4) {
                        break
                    }
                    try? await Task.sleep(for: .milliseconds(16))
                }
                guard let self, self.current === st, !Task.isCancelled else { return }
                self.onStage?(command)
            })
        case .photo(let id, let crop):
            if let i = st.answerIndex, i < turns.count, !turns[i].photos.contains(crop) { turns[i].photos.append(crop) }
            if let i = st.answerIndex, i < turns.count { turns[i].photoFor[id] = crop }
        case .done(_, let timings, let heldID):
            if !st.marks.isEmpty { eyes?.marks(st.marks) }
            if let sonic = st.sonic, !st.sonicFailed { sonic.say("", more: false) }
            // A held answer is applied only once confirmed (a dropped one never gets here): now it joins the
            // conversation on the gateway too, so later answers and the turn caps see it.
            if let heldID {
                let gateway = self.gateway, scan = scanID
                unsavedHeld = heldID
                pendingCommit = Task { [weak self] in
                    guard (try? await gateway.commitHeld(scanID: scan, heldID: heldID)) != nil else { return }
                    if self?.unsavedHeld == heldID { self?.unsavedHeld = nil }
                }
            } else if let carried = st.carried, unsavedHeld == carried {
                unsavedHeld = nil   // the gateway saved it with this turn
            }
            diag?.log("voice_turn", ["input": st.input, "eager_start": st.confirmedEarly,
                                     "voice_status": LeaVoice.shared.status.map { $0.cartesia ? "cartesia" : "no_cartesia" } ?? "none",
                                     "muted": LeaVoice.shared.muted,
                                     "voice": st.sonic == nil ? "gateway" : (st.sonicFailed ? "sonic_failed" : "sonic_ws"),
                                     "end_to_first_sentence_ms": st.firstSentence ?? -1,
                                     "end_to_first_audio_ms": firstAudioMs ?? -1, "server_ms": timings])
        }
    }

    // MARK: Captions in step with her voice

    /// Moves `heardSentence` along as the answer is heard: on the audio player's clock when there is audio, else on
    /// the time since her first sentence arrived at `charsPerSecond` (muted, the phone's voice).
    private func startCaptionClock(_ st: TurnState) {
        if heardSentence == nil { heardSentence = 0 }
        guard captionClock == nil else { return }
        captionClock = Task { [weak self] in
            while !Task.isCancelled {
                guard let self, self.current === st else { return }
                let played = LeaVoice.shared.playedSeconds(stream: st.id)
                    ?? st.firstHeardAt.map { Date().timeIntervalSince($0) } ?? 0
                let i = Self.heardIndex(starts: st.sentenceStarts, played: played)
                if self.heardSentence != i { self.heardSentence = i }
                try? await Task.sleep(for: .milliseconds(80))
            }
        }
    }

    private func stopCaptionClock() {
        captionClock?.cancel()
        captionClock = nil
        heardSentence = nil
    }

    /// The sentence being heard `played` seconds into the answer: the last one that has started (never before the
    /// first, never past the last that arrived).
    nonisolated static func heardIndex(starts: [Double], played: Double) -> Int {
        guard !starts.isEmpty else { return 0 }
        return max(0, (starts.lastIndex { $0 <= played + 0.05 }) ?? 0)
    }

    /// The words of `turn` as the screen shows them now: the answer being spoken up to the sentence being heard (the
    /// rest appears as she says it); every other turn whole.
    func shownText(_ turn: Turn) -> String {
        guard !turn.mine, let heard = heardSentence, turn.id == speakingTurnID, !turn.sentences.isEmpty else { return turn.text }
        return turn.sentences.prefix(heard + 1).joined(separator: " ")
    }

    /// The answer being spoken now (its captions follow `heardSentence`), if any.
    var speakingTurnID: UUID? {
        guard heardSentence != nil, let i = current?.answerIndex, i < turns.count else { return nil }
        return turns[i].id
    }

    /// Sonic's socket for one answer: audio straight into playback; if it fails, the rest is the phone's voice; the
    /// gateway's spend guard counts it (paused: the phone's voice).
    private func openSonic(_ grant: RealtimeGrant, _ st: TurnState) {
        let sonic = SonicSocket(grant: grant)
        let append = LeaVoice.shared.appender(stream: st.id)
        let start = st.start
        let first = FirstFlag()
        let markFirst: @Sendable () -> Void = { [weak self] in
            let ms = Self.ms(since: start)
            Task { @MainActor in if self?.firstAudioMs == nil { self?.firstAudioMs = ms } }
        }
        sonic.onAudio = { d in append(d, first.take() ? markFirst : nil) }
        sonic.onDone = { Task { @MainActor in st.sonicFinished = true } }
        sonic.onFailed = { [weak self] why in Task { @MainActor in
            self?.sonicFailed(st, heard: sonic.audioSeconds)
            self?.diag?.log("realtime_tts_failed", ["why": why])
        } }
        st.sonic = sonic
        let gateway = self.gateway
        Task {
            if !(await gateway.realtimeStream(kind: "tts")) {
                sonic.cancel()
                await MainActor.run { self.sonicFailed(st, heard: sonic.audioSeconds) }
            }
        }
    }

    /// Sonic stopped: the sentences it was given but the person has not heard yet are said by the phone (the gateway
    /// was asked for no audio on this path), then later sentences go to the phone too.
    private func sonicFailed(_ st: TurnState, heard seconds: Double) {
        guard !st.sonicFailed else { return }
        st.sonicFailed = true
        for say in Self.unheard(st.sonicSent, heardSeconds: seconds) { LeaVoice.shared.queueOnDevice(say, stream: st.id) }
        st.sonicSent = []
    }

    /// The sentences not yet heard after `heardSeconds` of audio, at about `charsPerSecond` of speech. The sentence
    /// cut off part-way is said again whole: repeating half a sentence beats losing it.
    nonisolated static func unheard(_ sent: [String], heardSeconds: Double, charsPerSecond: Double = 14) -> [String] {
        var end = 0.0
        return sent.filter { s in
            end += Double(s.count + 1) / charsPerSecond
            return end > heardSeconds + 0.05
        }
    }

    /// Speech rate for a sentence's estimated start when the audio carries no sentence boundaries (Sonic).
    static let charsPerSecond: Double = 14

    /// Longest a held early answer waits for the person's turn to end before it is dropped.
    static let maxHold: TimeInterval = 6

    /// What the person says is also their guess about what is wrong: the gateway turns it into a lead (404 while
    /// that route does not exist is ignored).
    private func postStory(_ text: String) {
        guard let sid = sessionID() else { return }
        let gateway = self.gateway, scan = scanID
        Task.detached { try? await gateway.postStory(sessionID: sid, text: text, scanID: scan) }
    }

    nonisolated private static func ms(since d: Date) -> Int { Int(Date().timeIntervalSince(d) * 1000) }
}

/// Which of Lea's streamed sentences the phone's own voice must say. A sentence the gateway sends with `audio: true`
/// is followed by its audio, unless Cartesia stops (refused, spend cap) first: then `audio_off` comes with no audio
/// for that sentence, and it would never be heard. The phone says it, then every later sentence (`audio: false`).
struct StreamFallback: Equatable {
    /// The last sentence sent with audio whose audio has not started yet.
    private(set) var awaitingAudio: String?

    /// A sentence arrived: the text to say on the device now, if any.
    mutating func sentence(_ text: String, withAudio: Bool) -> String? {
        awaitingAudio = withAudio ? text : nil
        return withAudio ? nil : text
    }

    /// Audio arrived: it belongs to the sentence awaiting it, which is heard.
    mutating func audio() { awaitingAudio = nil }

    /// Cartesia stopped: the sentence that got no audio, to say on the device (nil when its audio had started).
    mutating func audioOff() -> String? {
        defer { awaitingAudio = nil }
        return awaitingAudio
    }
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

/// True once (the first audio chunk of an answer), from any thread.
private final class FirstFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var fresh = true
    func take() -> Bool { lock.withLock { defer { fresh = false }; return fresh } }
}
