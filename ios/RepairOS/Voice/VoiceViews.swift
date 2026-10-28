import SwiftUI

/// The speaker toggle on the scan and report screens (remembered).
struct VoiceMuteButton: View {
    var onCamera = false
    @ObservedObject private var voice = LeaVoice.shared

    var body: some View {
        Button { voice.muted.toggle() } label: {
            Image(systemName: voice.muted ? "speaker.slash" : "speaker.wave.2")
                .symbolReplaceTransition()
        }
        .buttonStyle(RoundIconButtonStyle(onCamera: onCamera))
        .accessibilityLabel(voice.muted ? "Turn Lea's voice on" : "Mute Lea")
    }
}

/// Toolbar version of the toggle (the report's navigation bar).
struct VoiceMuteToolbarButton: View {
    @ObservedObject private var voice = LeaVoice.shared

    var body: some View {
        Button { voice.muted.toggle() } label: {
            Image(systemName: voice.muted ? "speaker.slash" : "speaker.wave.2")
        }
        .accessibilityLabel(voice.muted ? "Turn Lea's voice on" : "Mute Lea")
    }
}

// MARK: - Ask Lea on the report

/// The report's pinned Ask Lea bar is `ConversationPanel(skin: .bar)`; this keeps the words for a failed answer.
enum AskLeaBar {
    static func message(_ error: Error) -> String {
        if case GatewayError.http(503, _) = error { return "Answers are off on the server right now." }
        if case GatewayError.http(404, _) = error { return "This scan has no report on the server yet." }
        return "Lea couldn't answer: \(error.localizedDescription)"
    }
}

// MARK: - Dictating what's wrong on the home screen

/// A mic in the "What's wrong?" field: tap to dictate, tap again to stop (the same tap toggle as talking to Lea).
/// Words come from the gateway (`/v1/voice/stt`) when it has a voice key, else from the phone's own recognizer; they
/// are added to the field.
struct StoryMicButton: View {
    @Binding var text: String
    let gateway: GatewayClient?

    @State private var recorder = VoiceRecorder()
    @State private var toggle = TalkToggle()
    @State private var listening = false
    @State private var working = false

    var body: some View {
        Button(action: tap) {
            ZStack {
                if working {
                    ProgressView().controlSize(.small).tint(Palette.secondary)
                } else {
                    Image(systemName: listening ? "stop.circle.fill" : "mic")
                        .foregroundStyle(listening ? Palette.accent : Palette.secondary)
                        .symbolReplaceTransition()
                }
            }
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(working)
        .haptic(Haptic.light, trigger: listening)
        .accessibilityLabel(listening ? "Stop dictating" : "Dictate what's wrong")
        .accessibilityHint(listening ? "Adds what you said to the field" : "Tap and speak, then tap again")
        .onDisappear { if listening { listening = false; recorder.cancel() } }
    }

    private func tap() {
        toggle.sync(listening ? .listening : .idle)
        switch toggle.tap(at: Date()) {
        case .ignored, .interruptAndListen: return
        case .startListening:
            listening = true
            Task { if await !recorder.start() { listening = false } else if !listening { recorder.cancel() } }
        case .stopAndSend:
            listening = false
            guard let audio = recorder.stop() else { return }
            working = true
            Task {
                defer { working = false }
                var words = ""
                if LeaVoice.shared.status?.cartesia == true, let gateway {
                    words = (try? await gateway.stt(audio: audio)) ?? ""
                }
                if words.isEmpty { words = await recorder.transcribeOnDevice() }
                words = words.trimmingCharacters(in: .whitespacesAndNewlines)
                guard !words.isEmpty else { return }
                text = text.trimmingCharacters(in: .whitespaces).isEmpty ? words : text + " " + words
            }
        }
    }
}

// MARK: - Talking with Lea (conversation)

/// What the report's stage shows of the conversation (the caption strip, ReportStageView): the current exchange only,
/// your last question and the sentence Lea is on, plus the whole history for the transcript. Fed by the Ask Lea bar
/// (`ConversationPanel(skin: .bar)`), which owns the conversation.
@MainActor
final class LeaCaptionFeed: ObservableObject {
    @Published private(set) var question: String?
    /// Lea's newest sentence of this answer (her answer streams sentence by sentence, as it is spoken).
    @Published private(set) var sentence: String?
    @Published private(set) var thinking = false
    @Published private(set) var speaking = false
    @Published private(set) var failure: String?
    @Published private(set) var turns: [LeaConversation.Turn] = []
    /// Bumped on every change, so the stage can hide the strip after a quiet spell.
    @Published private(set) var revision = 0

    /// Something has been said or asked in this report's conversation.
    var started: Bool { question != nil || sentence != nil || thinking || failure != nil }

    func update(_ c: LeaConversation) {
        // The transcript shows what has been said: the answer being spoken only up to the sentence being heard.
        turns = c.turns.compactMap { t in
            var t = t
            t.text = c.shownText(t)
            return t.text.trimmedOrNil == nil ? nil : t
        }
        // The strip is this visit's exchange only: an answer from an earlier visit is in the transcript, not on the stage.
        let now = Self.exchange(turns: Array(c.turns.dropFirst(c.historyCount)), live: c.liveCaption, heard: c.heardSentence,
                                speaking: c.speakingTurnID)
        question = now.question
        sentence = now.sentence
        thinking = c.phase == .thinking
        speaking = c.phase == .speaking
        if case .failed(let m) = c.phase { failure = m } else { failure = nil }
        revision += 1
    }

    #if DEBUG
    /// Design review only (`--ui-lea-replay`): plays a recorded real turn's words through the strip.
    func replay(question: String?, sentence: String?, thinking: Bool, speaking: Bool, photoFor: [String: String] = [:],
                answer: [String]? = nil) {
        self.question = question
        self.sentence = sentence
        self.thinking = thinking
        self.speaking = speaking
        failure = nil
        var t: [LeaConversation.Turn] = []
        if let question { t.append(LeaConversation.Turn(mine: true, text: question)) }
        if let answer, !answer.isEmpty {
            var lea = LeaConversation.Turn(mine: false, text: answer.joined(separator: " "), sentences: answer)
            lea.photoFor = photoFor
            lea.photos = Array(photoFor.values)
            t.append(lea)
        }
        turns = t
        revision += 1
    }
    #endif

    /// The current exchange for the strip: your question (the live words while you talk) and the sentence of Lea's
    /// answer to it that you are hearing (`heard`, when `speaking` is that answer), else her last sentence. Her
    /// sentences are the ones she streamed, never re-split (a split at every full stop cut "1.5 mm" into "5 mm.").
    nonisolated static func exchange(turns: [LeaConversation.Turn], live: String, heard: Int?,
                         speaking: UUID?) -> (question: String?, sentence: String?) {
        if let words = live.trimmedOrNil { return (words, nil) }
        let lastMine = turns.lastIndex { $0.mine && $0.text.trimmedOrNil != nil }
        let lastLea = turns.lastIndex { !$0.mine && $0.text.trimmedOrNil != nil }
        let question = lastMine.map { turns[$0].text.trimmingCharacters(in: .whitespacesAndNewlines) }
        // Only an answer to that question (an older answer is not this exchange).
        guard let lea = lastLea, lea > (lastMine ?? -1) else { return (question, nil) }
        let answer = turns[lea]
        let said = answer.sentences.isEmpty ? LeaText.sentences(answer.text) : answer.sentences
        if let heard, answer.id == speaking, !said.isEmpty { return (question, said[min(heard, said.count - 1)]) }
        return (question, said.last)
    }
}

/// The conversation with Lea over one model and gesture (tap to talk, tap again to send; "Hey Lea" too), in two skins:
/// - `.talk` (the Talk to Lea sheet, DESIGN §7.7): captions inline on the canvas, newest at the bottom, the spot photos
///   Lea shows under her words; at the bottom her mark at 88 pt (the talk button: a stop badge while it listens), her
///   state in words and one help line; a keyboard button reveals "Type to Lea".
/// - `.bar` (the report's pinned bar): `[mark · Talk to Lea]` as a quiet capsule, a keyboard button, and Continue
///   scanning in blue when it can go; the one frosted bar on the screen. Captions go to the stage (`feed`).
struct ConversationPanel: View {
    enum Skin: Sendable { case talk, bar }

    @StateObject private var convo: LeaConversation
    let onAction: (String) -> Void
    var compact = false
    var skin: Skin = .talk
    var feed: LeaCaptionFeed?
    /// `.bar`: show Continue scanning (it calls `onAction("continue_scan")`).
    var continueScan = false
    /// Start listening as it opens (the home Talk button), as one tap on the talk button would.
    var listenOnOpen = false

    @ObservedObject private var wake = WakeListener.shared
    @State private var typed = ""
    @State private var typing = false
    @FocusState private var typeFocused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The spot photo open full screen (pinch to zoom).
    @State private var zoomed: SpotZoom?

    /// `onStage`: on a report, Lea's moves of its 3D model (show a part, a side, zoom).
    init(scanID: String?, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String? = { nil },
         compact: Bool = false, afterTurn: (@MainActor () -> Void)? = nil, onStage: ((StageCommand) -> Void)? = nil,
         skin: Skin = .talk, feed: LeaCaptionFeed? = nil, continueScan: Bool = false, listenOnOpen: Bool = false,
         onAction: @escaping (String) -> Void) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.afterTurn = afterTurn
        c.onStage = onStage
        c.acceptOffer = onAction   // "yes" to the offer on screen is the same as tapping it
        _convo = StateObject(wrappedValue: c)
        self.compact = compact
        self.skin = skin
        self.feed = feed
        self.continueScan = continueScan
        self.listenOnOpen = listenOnOpen
        self.onAction = onAction
    }

    var body: some View {
        Group {
            switch skin {
            case .talk: talkPanel
            case .bar: askBar
            }
        }
        .animation(Motion.pick(Motion.standard, reduceMotion: reduceMotion), value: convo.phase)
        .animation(Motion.pick(Motion.standard, reduceMotion: reduceMotion), value: convo.turns.count)
        .animation(Motion.pick(Motion.standard, reduceMotion: reduceMotion), value: typing)
        .onAppear { convo.appeared(); feed?.update(convo) }
        // The person asked for it in so many words: the tour starts on its own (after Lea's "I'll walk you through it").
        .onValueChange(of: convo.actionKind) { _, kind in if kind == "walkthrough" { onAction("walkthrough") } }
        .onValueChange(of: convo.turns) { feed?.update(convo) }
        .onValueChange(of: convo.phase) { feed?.update(convo) }
        .onValueChange(of: convo.liveCaption) { feed?.update(convo) }
        .onValueChange(of: convo.heardSentence) { feed?.update(convo) }
        .fullScreenCover(item: $zoomed) { z in CropZoomView(shas: z.shas, index: z.index) }
        .task {
            WakeListener.shared.capture(false)   // back from a scan: listening resumes here for sure
            await convo.loadHistory()
            feed?.update(convo)
            if let first = wake.pendingUtterance {
                wake.pendingUtterance = nil
                convo.heard(first)
            } else if listenOnOpen, convo.talkState == .idle {
                convo.tapTalk()
            }
        }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.cancelRecording(); convo.stopLea() }
        }
    }

    private var isFailed: Bool { if case .failed = convo.phase { true } else { false } }
    /// Someone is being heard: the button's recording, or a "Hey Lea" turn.
    private var listening: Bool { convo.recording || convo.phase == .listening || wake.mode == .followUp }

    /// Lea's mark follows her real state (no voice level is plumbed yet, so listening and speaking are static).
    private var markState: LeaMarkView.Phase {
        if listening { return .listening }
        switch convo.phase {
        case .thinking: return .thinking
        case .speaking: return .speaking
        default: return .idle
        }
    }

    private func sendTyped() {
        let words = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return }
        typed = ""
        convo.typed(words)
    }


    // MARK: Talk sheet skin

    private var talkPanel: some View {
        VStack(spacing: Spacing.m) {
            captions
            if let action = convo.action, action.kind != "walkthrough" {
                Button { onAction(action.kind) } label: {
                    Label(action.label, systemImage: action.kind == "start_scan" ? Symbol.scan : Symbol.closeup)
                }
                .buttonStyle(.primary(.quiet))
                .padding(.horizontal, Spacing.gutter)
                .transition(.opacity)
            }
            if typing { typeField.padding(.horizontal, Spacing.gutter) }
            VStack(spacing: Spacing.xs) {
                ZStack {
                    Button { convo.tapTalk() } label: {
                        LeaMarkView(state: markState, size: 88)
                            .overlay(alignment: .bottomTrailing) { if convo.recording { StopBadge(size: 30) } }
                            .frame(width: 104, height: 104)
                            .contentShape(Circle())
                    }
                    .buttonStyle(TalkButtonStyle())
                    .haptic(Haptic.light, trigger: convo.recording)
                    .accessibilityLabel(convo.recording ? "Stop listening" : "Talk to Lea")
                    .accessibilityValue(stateWords)
                    .accessibilityHint(convo.recording ? "Sends what you said to Lea" : "Tap and ask, then tap again to send")
                    HStack {
                        Spacer()
                        Button { typing.toggle(); typeFocused = typing } label: { Image(systemName: typing ? "keyboard.chevron.compact.down" : "keyboard") }
                            .buttonStyle(RoundIconButtonStyle())
                            .accessibilityLabel(typing ? "Hide the keyboard" : "Type to Lea")
                    }
                    .padding(.horizontal, Spacing.gutter)
                }
                Text(stateWords).font(.headline).foregroundStyle(Palette.ink)
                    .contentTransition(.opacity)
                    .accessibilityHidden(true)
                Text(convo.recording ? "Tap again when you're done." : "Tap to talk. Tap again to send.")
                    .font(.subheadline).foregroundStyle(Palette.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, Spacing.gutter)
            }
            .padding(.bottom, Spacing.s)
        }
    }

    private var stateWords: String {
        if convo.recording || listening { return "Listening…" }
        switch convo.phase {
        case .thinking: return "Thinking…"
        case .speaking: return "Speaking"
        default: return "Talk to Lea"
        }
    }

    /// Captions inline on the canvas: "You" / "Lea" over the words, newest at the bottom, auto-scrolled. No box.
    private var captions: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ForEach(convo.turns.suffix(compact ? 4 : 30).filter { convo.shownText($0).trimmedOrNil != nil }) { t in
                        CaptionLine(speaker: t.mine ? .you : .lea, text: convo.shownText(t)).id(t.id)
                        if !t.photos.isEmpty {
                            SpotPhotos(shas: t.photos) { i in zoomed = SpotZoom(shas: t.photos, index: i) }
                                .id("photos:\(t.id)")
                        }
                    }
                    if !convo.liveCaption.isEmpty {
                        CaptionLine(speaker: .you, text: convo.liveCaption, live: true).id("live")
                    }
                    if case .failed(let m) = convo.phase {
                        Label { Text(m) } icon: { Image(systemName: "exclamationmark.bubble") }
                            .font(.subheadline).foregroundStyle(Palette.caution).id("failed")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Spacing.gutter)
                .padding(.vertical, Spacing.s)
            }
            .bounceBasedOnSize()
            .startsAtBottom()
            .onValueChange(of: convo.turns.last?.text) { _, _ in withAnimation(Motion.fade) { proxy.scrollTo(convo.turns.last?.id, anchor: .bottom) } }
            .onValueChange(of: convo.heardSentence) { _, _ in withAnimation(Motion.fade) { proxy.scrollTo(convo.turns.last?.id, anchor: .bottom) } }
            .onValueChange(of: isFailed) { _, failed in if failed { withAnimation(Motion.fade) { proxy.scrollTo("failed", anchor: .bottom) } } }
            .onValueChange(of: convo.turns.last?.photos) { _, photos in
                guard let last = convo.turns.last, !(photos ?? []).isEmpty else { return }
                withAnimation(Motion.fade) { proxy.scrollTo("photos:\(last.id)", anchor: .bottom) }
            }
            .onValueChange(of: convo.liveCaption) { _, _ in proxy.scrollTo("live", anchor: .bottom) }
        }
    }

    private var typeField: some View {
        // Detail that is easier to type (a model number, an error code): the same turn as saying it.
        FieldWell(symbol: "keyboard") {
            TextField("Type to Lea", text: $typed)
                .focused($typeFocused)
                .submitLabel(.send)
                .onSubmit(sendTyped)
        }
    }

    // MARK: Report bar skin

    private var askBar: some View {
        VStack(spacing: Spacing.s) {
            if let last = convo.turns.last, !last.mine, !last.photos.isEmpty {
                // The real photos of what Lea is showing, ringed by the gateway: tap to open full screen.
                SpotPhotos(shas: last.photos, size: 72) { i in zoomed = SpotZoom(shas: last.photos, index: i) }
                    .padding(.horizontal, Spacing.gutter)
                    .transition(.opacity)
            }
            if let action = convo.action, action.kind != "walkthrough" {
                Button { onAction(action.kind) } label: {
                    Label(action.label, systemImage: action.kind == "start_scan" ? Symbol.scan : Symbol.closeup)
                }
                .buttonStyle(.primary(.quiet))
                .padding(.horizontal, Spacing.gutter)
                .transition(.opacity)
            }
            GlassBar {
                if typing {
                    HStack(spacing: Spacing.s) {
                        typeField
                        Button { typing = false; typeFocused = false } label: { Image(systemName: Symbol.close) }
                            .buttonStyle(RoundIconButtonStyle())
                            .accessibilityLabel("Close typing")
                    }
                } else {
                    HStack(spacing: Spacing.s) {
                        askCapsule
                        Button { typing = true; typeFocused = true } label: { Image(systemName: "keyboard") }
                            .buttonStyle(RoundIconButtonStyle())
                            .accessibilityLabel("Type to Lea")
                        if continueScan {
                            Button { onAction("continue_scan") } label: { Text("Continue scanning") }
                                .buttonStyle(.primary)
                        }
                    }
                }
            }
        }
    }

    /// Tap to talk: the mark and the words in a quiet glass capsule (the same toggle as the sheet's mark); while it
    /// listens, a stop icon and the accent edge.
    private var askCapsule: some View {
        Button { convo.tapTalk() } label: {
            HStack(spacing: Spacing.s) {
                if convo.recording {
                    Image(systemName: "stop.circle.fill").font(.title3).foregroundStyle(Palette.accent)
                        .accessibilityHidden(true)
                } else {
                    LeaMarkView(state: markState, size: 24).accessibilityHidden(true)
                }
                Text(convo.recording ? "Listening… tap to send" : "Talk to Lea")
                    .contentTransition(.opacity)
            }
            .font(.buttonLabel)
            .foregroundStyle(Palette.ink)
            .lineLimit(1)
            .minimumScaleFactor(0.75)
            .padding(.horizontal, Spacing.l)
            .frame(maxWidth: .infinity, minHeight: 56)
            .background { Color.clear.glass(.card, in: Capsule()) }
            .overlay(Capsule().strokeBorder(Palette.accent, lineWidth: Line.drawing).opacity(convo.recording ? 1 : 0))
            .contentShape(Capsule())
        }
        .buttonStyle(TalkButtonStyle())
        .haptic(Haptic.light, trigger: convo.recording)
        .accessibilityLabel(convo.recording ? "Stop listening" : "Talk to Lea")
        .accessibilityHint(convo.recording ? "Sends what you said to Lea" : "Tap and ask about this scan, then tap again to send")
    }
}

/// Everything you and Lea said about this scan (tap the caption strip on the report): captions, newest at the bottom,
/// with the photos she showed.
struct LeaTranscriptSheet: View {
    @ObservedObject var feed: LeaCaptionFeed
    @Environment(\.dismiss) private var dismiss
    @State private var zoomed: SpotZoom?

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: Spacing.m) {
                        ForEach(feed.turns) { t in
                            CaptionLine(speaker: t.mine ? .you : .lea, text: t.text).id(t.id)
                            if !t.photos.isEmpty {
                                SpotPhotos(shas: t.photos) { i in zoomed = SpotZoom(shas: t.photos, index: i) }
                            }
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(Spacing.gutter)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .startsAtBottom()
                // New words while the transcript is open: it follows the latest line.
                .onValueChange(of: feed.turns.last?.text) { _, _ in withAnimation(Motion.fade) { proxy.scrollTo("end", anchor: .bottom) } }
            }
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle("Lea").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
            .fullScreenCover(item: $zoomed) { z in CropZoomView(shas: z.shas, index: z.index) }
        }
        .tint(Palette.accent)
        .presentationDetents([.medium, .large])
    }
}

/// A small ear while "Hey Lea" is listening (the system's mic dot shows too). No pulse: the words say it.
struct WakeIndicator: View {
    var onCamera = false
    @ObservedObject private var wake = WakeListener.shared

    var body: some View {
        if wake.mode != .off {
            Image(systemName: wake.mode == .followUp ? "ear.badge.waveform" : "ear")
                .font(.system(size: 15, weight: .regular))
                .foregroundStyle(onCamera ? Palette.next : Palette.accent)
                .frame(width: 44, height: 44)
                .background {
                    if onCamera { SmokeFill(shape: Circle()) } else { Color.clear.glass(.card, in: Circle()) }
                }
                .accessibilityLabel(wake.mode == .followUp ? "Lea is listening" : "Listening for Hey Lea")
                .transition(.opacity)
        }
    }
}

/// The home screen's conversation, before any scan: opened by "Hey Lea" or the Talk button (already listening). Lea
/// hears what is wrong, and the scan then starts in the same session (Voice/PreScan), so the conversation carries on at
/// the report. DESIGN §7.7: Start scan in blue, captions inline, Lea's mark at the bottom.
struct HomeConversationSheet: View {
    let gateway: GatewayClient
    /// Opened by the Talk button: Lea is listening at once (the talk button's recording; a tap on her mark sends).
    var listenOnOpen = false
    let startScan: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @ObservedObject private var pre = PreScan.shared

    var body: some View {
        NavigationStack {
            VStack(spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Text("Tell Lea what's wrong. The scan is judged against the product's design (Settings, Product).")
                        .font(.subheadline).foregroundStyle(Palette.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Button { begin() } label: { Label("Start scan", systemImage: Symbol.scan) }
                        .buttonStyle(.primary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Spacing.gutter)
                ConversationPanel(scanID: pre.scanID, gateway: gateway, diag: WakeListener.diag,
                                  sessionID: { PreScan.shared.sessionID }, listenOnOpen: listenOnOpen) { kind in
                    if kind == "start_scan" { begin() }
                }
                .id(pre.scanID)
            }
            .padding(.top, Spacing.s)
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle("Lea").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
            .task {
                await pre.begin(gateway: gateway)
                // Opened by "Hey Siri, hey lea": Lea speaks first, then listens (hands-free).
                if IntentInbox.shared.takeGreeting() { LeaConversation.active?.leaSays("Hey, what's up?", listenAfter: true) }
            }
        }
        .tint(Palette.accent)
        .presentationDetents([.large])
    }

    private func begin() {
        let said = LeaConversation.active?.turns.filter(\.mine).map(\.text).joined(separator: " ") ?? ""
        dismiss()
        startScan(said)
    }
}

// MARK: - Talking with Lea while scanning

/// Lea over the camera while scanning: the same conversation as this scan's report (it carries on there), up to two
/// lines of captions (the sentence she is saying), an ear that turns continuous hands-free on and off, and the talk
/// button (tap to talk, tap again to send; a stop icon while it listens). Each turn tells Lea what the scan screen
/// shows now (`live`), so she can coach while you scan. Small, so the camera stays clear.
struct ScanTalkBar: View {
    @StateObject private var convo: LeaConversation
    @ObservedObject private var wake = WakeListener.shared

    init(scanID: String, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String?,
         live: @escaping () -> [String: String], eyes: LeaEyes? = nil) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.live = live
        c.eyes = eyes
        _convo = StateObject(wrappedValue: c)
    }

    var body: some View {
        HStack(spacing: Spacing.s) {
            Button { wake.continuous.toggle() } label: {
                Image(systemName: wake.continuous ? "ear.badge.waveform" : "ear")
                    .font(.system(size: 15, weight: .regular))
                    .foregroundStyle(wake.continuous ? Palette.next : Palette.chalk)
                    .frame(width: 44, height: 44)
            }
            .accessibilityLabel(wake.continuous ? "Hands-free on" : "Hands-free off")
            Text(caption)
                .font(.footnote.weight(.medium))
                .foregroundStyle(isFailed ? Palette.cameraCaution : Palette.chalk)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentTransition(.opacity)
                .animation(Motion.caption, value: caption)
            // Lea's mark is the talk button over the camera (her state, never fake movement); a stop icon while it listens.
            Button { convo.tapTalk() } label: {
                Group {
                    if convo.recording {
                        Image(systemName: "stop.fill").font(.system(size: 13, weight: .bold)).foregroundStyle(Palette.cameraGlyph)
                            .frame(width: 24, height: 24)
                            .background(Circle().fill(Palette.next))
                    } else {
                        LeaMarkView(state: markState, size: 24, tint: Palette.next)
                    }
                }
                .padding(10)
                .contentShape(Circle())
            }
            .buttonStyle(TalkButtonStyle())
            .haptic(Haptic.light, trigger: convo.recording)
            .accessibilityLabel(convo.recording ? "Stop listening" : "Talk to Lea")
            .accessibilityHint(convo.recording ? "Sends what you said to Lea" : "Tap and ask, then tap again to send")
        }
        .padding(.leading, Spacing.xs)
        .padding(.trailing, Spacing.xxs)
        .padding(.vertical, Spacing.xxs)
        .background { SmokeFill(shape: Capsule()) }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .contentShape(Capsule())
        .environment(\.colorScheme, .dark)
        .onAppear {
            convo.appeared()
            if wake.continuous { wake.listenForFollowUp() }   // hands-free carries on into the scan
        }
        .task { await convo.loadHistory() }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.cancelRecording(); convo.stopLea() }
        }
    }

    private var listening: Bool { convo.recording || convo.phase == .listening || wake.mode == .followUp }
    private var isFailed: Bool { if case .failed = convo.phase { true } else { false } }

    private var markState: LeaMarkView.Phase {
        if listening { return .listening }
        switch convo.phase {
        case .thinking: return .thinking
        case .speaking: return .speaking
        default: return .idle
        }
    }

    private var caption: String {
        if case .failed(let m) = convo.phase { return m }
        if !convo.liveCaption.isEmpty { return convo.liveCaption }
        if convo.recording { return "Listening… tap the mic again to send." }
        if convo.phase == .thinking { return "Thinking…" }
        // The sentence she is saying (the whole answer clipped to two lines only ever showed its beginning).
        if convo.phase == .speaking || !listening,
           let sentence = LeaCaptionFeed.exchange(turns: convo.turns, live: "", heard: convo.heardSentence,
                                                   speaking: convo.speakingTurnID).sentence {
            return sentence
        }
        return listening ? "Lea is listening. Ask anything while you scan." : "Tap the mic to talk to Lea."
    }
}

// MARK: - The talk button

/// The talk button's press feedback (a small scale and fade; nothing under Reduce Motion but the fade).
struct TalkButtonStyle: ButtonStyle {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed && !reduceMotion ? 0.95 : 1)
            .opacity(configuration.isPressed ? 0.85 : 1)
            .animation(Motion.press, value: configuration.isPressed)
    }
}

/// The stop badge on Lea's mark while the talk button listens: tap again to send.
struct StopBadge: View {
    var size: CGFloat = 28
    var body: some View {
        Image(systemName: "stop.fill")
            .font(.system(size: size * 0.42, weight: .bold))
            .foregroundStyle(Palette.onAccent)
            .frame(width: size, height: size)
            .background(Circle().fill(Palette.accent))
            .overlay(Circle().strokeBorder(Palette.canvas, lineWidth: 2))
            .accessibilityHidden(true)
    }
}


// MARK: - The photo of what Lea showed

/// The real photo of each spot Lea showed in an answer, ringed where it is (the gateway crops it from the closest,
/// sharpest still that sees the spot; with none, the 3D model close on it). Evidence keeps its colour: a hairline and
/// the card radius only, and an expand glyph on smoke. Tap to open it full screen.
struct SpotPhotos: View {
    let shas: [String]
    var size: CGFloat = 112
    let open: (Int) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: Spacing.s) {
                ForEach(Array(shas.enumerated()), id: \.element) { i, sha in
                    Button { open(i) } label: {
                        CropThumb(sha: sha, cornerRadius: Radius.medium)
                            .frame(width: size, height: size)
                            .overlay(alignment: .bottomTrailing) {
                                Image(systemName: "arrow.up.left.and.arrow.down.right").font(.caption2.weight(.bold))
                                    .foregroundStyle(Palette.chalk).padding(5)
                                    .background(Palette.smoke, in: Circle()).padding(5)
                            }
                    }
                    .buttonStyle(PressableCardStyle())
                    .accessibilityLabel("Photo of the spot Lea showed")
                    .accessibilityHint("Opens it full screen")
                    .transition(.opacity)
                }
            }
        }
        .frame(height: size)
    }
}

struct SpotZoom: Identifiable {
    let id = UUID()
    let shas: [String]
    let index: Int
}
