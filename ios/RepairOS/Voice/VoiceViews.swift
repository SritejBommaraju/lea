import SwiftUI

/// The speaker toggle on the scan and report screens (remembered).
struct VoiceMuteButton: View {
    var onCamera = false
    @State private var voice = LeaVoice.shared

    var body: some View {
        Button { voice.muted.toggle() } label: {
            Image(systemName: voice.muted ? "speaker.slash" : "speaker.wave.2")
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(RoundIconButtonStyle(onCamera: onCamera))
        .accessibilityLabel(voice.muted ? "Turn Lea's voice on" : "Mute Lea")
    }
}

/// Toolbar version of the toggle (the report's navigation bar).
struct VoiceMuteToolbarButton: View {
    @State private var voice = LeaVoice.shared

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

/// A mic in the "What's wrong?" field: hold to dictate. Words come from the gateway (`/v1/voice/stt`) when it has a
/// voice key, else from the phone's own recognizer; they are added to the field.
struct StoryMicButton: View {
    @Binding var text: String
    let gateway: GatewayClient?

    @State private var recorder = VoiceRecorder()
    @State private var held = false
    @State private var listening = false
    @State private var working = false

    var body: some View {
        ZStack {
            if working {
                ProgressView().controlSize(.small).tint(Palette.secondary)
            } else {
                // The waveform only while really recording (the person is holding it).
                Image(systemName: listening ? "waveform" : "mic")
                    .foregroundStyle(listening ? Palette.accent : Palette.secondary)
                    .contentTransition(.symbolEffect(.replace))
            }
        }
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
        .gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in if !held { held = true; press() } }
            .onEnded { _ in held = false; release() })
        .sensoryFeedback(.impact(weight: .light), trigger: listening) { _, new in new }   // on press only
        .accessibilityLabel("Dictate what's wrong")
        .accessibilityHint("Hold and speak, then let go")
        .accessibilityAddTraits(.isButton)
    }

    private func press() {
        listening = true
        Task {
            if await !recorder.start() { listening = false } else if !held { release() }
        }
    }

    private func release() {
        guard listening else { return }
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

// MARK: - Talking with Lea (conversation)

/// What the report's stage shows of the conversation (the caption strip, ReportStageView): the current exchange only,
/// your last question and the sentence Lea is on, plus the whole history for the transcript. Fed by the Ask Lea bar
/// (`ConversationPanel(skin: .bar)`), which owns the conversation.
@MainActor
@Observable
final class LeaCaptionFeed {
    private(set) var question: String?
    /// Lea's newest sentence of this answer (her answer streams sentence by sentence, as it is spoken).
    private(set) var sentence: String?
    private(set) var thinking = false
    private(set) var speaking = false
    private(set) var failure: String?
    private(set) var turns: [LeaConversation.Turn] = []
    /// Bumped on every change, so the stage can hide the strip after a quiet spell.
    private(set) var revision = 0

    /// Something has been said or asked in this report's conversation.
    var started: Bool { question != nil || sentence != nil || thinking || failure != nil }

    func update(_ c: LeaConversation) {
        turns = c.turns
        let live = c.liveCaption.trimmingCharacters(in: .whitespacesAndNewlines)
        let lastLea = c.turns.lastIndex { !$0.mine }
        let lastMine = c.turns.lastIndex { $0.mine }
        if !live.isEmpty {
            question = live
            sentence = nil
        } else {
            question = lastMine.map { c.turns[$0].text }
            // Only an answer to that question (an older answer is not this exchange).
            if let lea = lastLea, lea > (lastMine ?? -1) { sentence = Self.lastSentence(c.turns[lea].text) } else { sentence = nil }
        }
        thinking = c.phase == .thinking
        speaking = c.phase == .speaking
        if case .failed(let m) = c.phase { failure = m } else { failure = nil }
        revision += 1
    }

    #if DEBUG
    /// Design review only (`--ui-lea-replay`): plays a recorded real turn's words through the strip.
    func replay(question: String?, sentence: String?, thinking: Bool, speaking: Bool, photoFor: [String: String] = [:],
                answer: String? = nil) {
        self.question = question
        self.sentence = sentence
        self.thinking = thinking
        self.speaking = speaking
        failure = nil
        var t: [LeaConversation.Turn] = []
        if let question { t.append(LeaConversation.Turn(mine: true, text: question)) }
        if let answer { var lea = LeaConversation.Turn(mine: false, text: answer); lea.photoFor = photoFor; lea.photos = Array(photoFor.values); t.append(lea) }
        turns = t
        revision += 1
    }
    #endif

    /// The last sentence of `text` ("…the third one from the left on the top header.").
    static func lastSentence(_ text: String) -> String? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return nil }
        var parts: [String] = []
        var current = ""
        for ch in t {
            current.append(ch)
            if ".!?".contains(ch) {
                let s = current.trimmingCharacters(in: .whitespaces)
                if !s.isEmpty { parts.append(s) }
                current = ""
            }
        }
        let rest = current.trimmingCharacters(in: .whitespaces)
        if !rest.isEmpty { parts.append(rest) }
        return parts.last
    }
}

/// The conversation with Lea over one model and gesture (hold to talk; "Hey Lea" too), in two skins:
/// - `.talk` (the Talk to Lea sheet, DESIGN §7.7): captions inline on the canvas, newest at the bottom, the spot photos
///   Lea shows under her words; at the bottom her mark at 88 pt (the hold target), her state in words and one help
///   line; a keyboard button reveals "Type to Lea".
/// - `.bar` (the report's pinned bar): `[mark · Hold to ask Lea]` as a quiet capsule, a keyboard button, and Continue
///   scanning in violet when it can go; the one frosted bar on the screen. Captions go to the stage (`feed`).
struct ConversationPanel: View {
    enum Skin: Sendable { case talk, bar }

    @State private var convo: LeaConversation
    let onAction: (String) -> Void
    var compact = false
    var skin: Skin = .talk
    var feed: LeaCaptionFeed?
    /// `.bar`: show Continue scanning (it calls `onAction("continue_scan")`).
    var continueScan = false

    @State private var wake = WakeListener.shared
    @State private var pressedAt: Date?
    @State private var typed = ""
    @State private var typing = false
    @FocusState private var typeFocused: Bool
    /// A tap instead of a hold: the button says how to talk for a moment.
    @State private var tapHint = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// The spot photo open full screen (pinch to zoom).
    @State private var zoomed: SpotZoom?

    /// `onStage`: on a report, Lea's moves of its 3D model (show a part, a side, zoom).
    init(scanID: String?, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String? = { nil },
         compact: Bool = false, afterTurn: (@MainActor () -> Void)? = nil, onStage: ((StageCommand) -> Void)? = nil,
         skin: Skin = .talk, feed: LeaCaptionFeed? = nil, continueScan: Bool = false,
         onAction: @escaping (String) -> Void) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.afterTurn = afterTurn
        c.onStage = onStage
        c.acceptOffer = onAction   // "yes" to the offer on screen is the same as tapping it
        _convo = State(initialValue: c)
        self.compact = compact
        self.skin = skin
        self.feed = feed
        self.continueScan = continueScan
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
        .onChange(of: convo.actionKind) { _, kind in if kind == "walkthrough" { onAction("walkthrough") } }
        .onChange(of: convo.turns) { feed?.update(convo) }
        .onChange(of: convo.phase) { feed?.update(convo) }
        .onChange(of: convo.liveCaption) { feed?.update(convo) }
        .fullScreenCover(item: $zoomed) { z in CropZoomView(shas: z.shas, index: z.index) }
        .task {
            WakeListener.shared.capture(false)   // back from a scan: listening resumes here for sure
            await convo.loadHistory()
            feed?.update(convo)
            if let first = wake.pendingUtterance {
                wake.pendingUtterance = nil
                convo.heard(first)
            }
        }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.stopLea() }
        }
    }

    private var isFailed: Bool { if case .failed = convo.phase { true } else { false } }
    private var listening: Bool { convo.phase == .listening || wake.mode == .followUp }

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

    private func showTapHint() {
        // A tap is not a question (the owner, 2026-10-25: press and hold to talk; the tap-for-hands-free mode
        // misfired): say how. "Hey Lea" still works hands-free.
        withAnimation(Motion.fade) { tapHint = true }
        Task { try? await Task.sleep(for: .seconds(1.6)); withAnimation(Motion.fade) { tapHint = false } }
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
                    LeaMarkView(state: markState, size: 88)
                        .scaleEffect(pressedAt != nil && !reduceMotion ? 0.94 : 1)
                        .animation(Motion.press, value: pressedAt != nil)
                        .frame(width: 104, height: 104)
                        .contentShape(Circle())
                        .holdOrTapToTalk(convo, pressedAt: $pressedAt, tap: showTapHint)
                        .sensoryFeedback(.impact(weight: .light), trigger: pressedAt != nil) { _, new in new }   // on press only
                        .accessibilityLabel("Talk to Lea")
                        .accessibilityValue(stateWords)
                        .accessibilityHint("Press and hold, ask, then let go")
                        .accessibilityAddTraits(.isButton)
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
                Text("Hold to talk. Lea answers when you let go.")
                    .font(.subheadline).foregroundStyle(Palette.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, Spacing.gutter)
            }
            .padding(.bottom, Spacing.s)
        }
    }

    private var stateWords: String {
        if tapHint { return "Hold to talk" }
        if listening { return "Listening…" }
        switch convo.phase {
        case .thinking: return "Thinking…"
        case .speaking: return "Speaking"
        default: return "Hold to talk"
        }
    }

    /// Captions inline on the canvas: "You" / "Lea" over the words, newest at the bottom, auto-scrolled. No box.
    private var captions: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ForEach(convo.turns.suffix(compact ? 4 : 30)) { t in
                        CaptionLine(speaker: t.mine ? .you : .lea, text: t.text).id(t.id)
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
            .scrollBounceBehavior(.basedOnSize)
            .defaultScrollAnchor(.bottom)
            .onChange(of: convo.turns.last?.text) { _, _ in withAnimation(Motion.fade) { proxy.scrollTo(convo.turns.last?.id, anchor: .bottom) } }
            .onChange(of: convo.turns.last?.photos) { _, photos in
                guard let last = convo.turns.last, !(photos ?? []).isEmpty else { return }
                withAnimation(Motion.fade) { proxy.scrollTo("photos:\(last.id)", anchor: .bottom) }
            }
            .onChange(of: convo.liveCaption) { _, _ in proxy.scrollTo("live", anchor: .bottom) }
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

    /// Hold to ask: the mark and the words in a quiet glass capsule (the same gesture as the sheet's mark).
    private var askCapsule: some View {
        HStack(spacing: Spacing.s) {
            LeaMarkView(state: markState, size: 24).accessibilityHidden(true)
            Text(tapHint ? "Hold to talk" : (listening ? "Listening… let go to ask" : "Hold to ask Lea"))
                .contentTransition(.opacity)
        }
        .font(.buttonLabel)
        .foregroundStyle(Palette.ink)
        .lineLimit(1)
        .minimumScaleFactor(0.75)
        .padding(.horizontal, Spacing.l)
        .frame(maxWidth: .infinity, minHeight: 56)
        .background { Color.clear.glass(.card, in: Capsule()) }
        .overlay(Capsule().strokeBorder(Palette.accent, lineWidth: Line.drawing).opacity(listening ? 1 : 0))
        .scaleEffect(pressedAt != nil && !reduceMotion ? 0.97 : 1)
        .animation(Motion.press, value: pressedAt != nil)
        .contentShape(Capsule())
        .holdOrTapToTalk(convo, pressedAt: $pressedAt, tap: showTapHint)
        .sensoryFeedback(.impact(weight: .light), trigger: pressedAt != nil) { _, new in new }   // on press only
        .accessibilityLabel("Hold to ask Lea")
        .accessibilityHint("Press and hold, ask about this scan, then let go")
        .accessibilityAddTraits(.isButton)
    }
}

/// Everything you and Lea said about this scan (tap the caption strip on the report): captions, newest at the bottom,
/// with the photos she showed.
struct LeaTranscriptSheet: View {
    let feed: LeaCaptionFeed
    @Environment(\.dismiss) private var dismiss
    @State private var zoomed: SpotZoom?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    ForEach(feed.turns) { t in
                        CaptionLine(speaker: t.mine ? .you : .lea, text: t.text)
                        if !t.photos.isEmpty {
                            SpotPhotos(shas: t.photos) { i in zoomed = SpotZoom(shas: t.photos, index: i) }
                        }
                    }
                }
                .padding(Spacing.gutter)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .defaultScrollAnchor(.bottom)
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
    @State private var wake = WakeListener.shared

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
/// works out what the object is (what the person says, plus an optional photo of the label), says what public sources
/// report about it, and the scan then starts in the same session (Voice/PreScan), so the conversation carries on at
/// the report. DESIGN §7.7: the brief as a card, the label photo as a quiet button that becomes a status line, Start
/// scan in violet, captions inline, Lea's mark at the bottom.
struct HomeConversationSheet: View {
    let gateway: GatewayClient
    let startScan: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var pre = PreScan.shared
    @State private var camera = false
    @State private var spokenIdentity: String?
    @State private var spokenBrief: ProductBrief?
    @State private var saidLookingUp = false

    var body: some View {
        NavigationStack {
            VStack(spacing: Spacing.m) {
                VStack(alignment: .leading, spacing: Spacing.m) {
                    Text("Tell Lea what it is and what's wrong. She'll work out the exact product before you scan.")
                        .font(.subheadline).foregroundStyle(Palette.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let brief = pre.live?.brief { briefCard(brief) }
                    labelControl
                    if case .failed(let m) = pre.label {
                        Text(m).font(.footnote).foregroundStyle(Palette.caution)
                    }
                    Button { begin() } label: { Label("Start scan", systemImage: Symbol.scan) }
                        .buttonStyle(.primary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Spacing.gutter)
                ConversationPanel(scanID: pre.scanID, gateway: gateway, diag: WakeListener.diag,
                                  sessionID: { PreScan.shared.sessionID }, afterTurn: lookingUp) { kind in
                    if kind == "start_scan" { begin() }
                }
                .id(pre.scanID)
            }
            .padding(.top, Spacing.s)
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle("Lea").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.fontWeight(.semibold) } }
            .fullScreenCover(isPresented: $camera) {
                CameraPicker { image in
                    camera = false
                    if let image { Task { await pre.sendLabel(image, gateway: gateway) } }
                }
                .ignoresSafeArea()
            }
            .task {
                await pre.begin(gateway: gateway)
                // Opened by "Hey Siri, hey lea": Lea speaks first, then listens (hands-free).
                if IntentInbox.shared.takeGreeting() { LeaConversation.active?.leaSays("Hey, what's up?") }
            }
            // What the backend works out is said once, in Lea's voice, and shown in the conversation.
            .onChange(of: pre.live?.identity?.words) { _, words in
                guard let words, words != spokenIdentity, pre.live?.brief == nil else { return }
                spokenIdentity = words
                LeaConversation.active?.leaSays("This looks like \(words).")
            }
            .onChange(of: pre.live?.brief) { _, brief in
                guard let brief, brief != spokenBrief else { return }
                spokenBrief = brief
                LeaConversation.active?.leaSays(brief.spoken)
            }
        }
        .tint(Palette.accent)
        .presentationDetents([.large])
    }

    /// After the first answer, while the brief is being researched (about half a minute): say so, once.
    private func lookingUp() {
        guard !saidLookingUp, pre.live?.brief == nil, pre.sessionID != nil else { return }
        saidLookingUp = true
        LeaConversation.active?.leaSays("Let me look that up while we talk. It takes about half a minute.")
    }

    /// "Show me the label" while it can be sent; a spinner while sending; once sent, a plain status line (a status is
    /// never a button).
    @ViewBuilder private var labelControl: some View {
        if pre.label == .sent {
            Label("Label photo sent", systemImage: "checkmark")
                .font(.subheadline.weight(.semibold)).foregroundStyle(Palette.secondary)
                .frame(minHeight: 44)
        } else {
            Button { camera = true } label: {
                if pre.label == .sending {
                    HStack(spacing: Spacing.s) { ProgressView().tint(Palette.ink); Text("Sending…") }
                } else {
                    Label("Show me the label", systemImage: "camera.viewfinder")
                }
            }
            .buttonStyle(.primary(.quiet))
            .disabled(pre.sessionID == nil || pre.label == .sending)
        }
    }

    /// The product brief: the product as the title, then ruled rows for the known issues and the sources.
    private func briefCard(_ b: ProductBrief) -> some View {
        var rows = b.knownIssues.prefix(3).map { RuledRows.Row("Known issue", $0) }
        if !b.sources.isEmpty { rows.append(RuledRows.Row("From", b.sources.prefix(3).joined(separator: ", "))) }
        return VStack(alignment: .leading, spacing: Spacing.s) {
            Text([b.brand, b.model, b.name].compactMap { $0 }.joined(separator: " ")).display(.card).foregroundStyle(Palette.ink)
            if !rows.isEmpty { RuledRows(rows: rows) }
        }
        .padding(Spacing.l)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glass(.card, in: RoundedRectangle(cornerRadius: Radius.card, style: .continuous))
        .accessibilityElement(children: .combine)
    }

    private func begin() {
        let said = LeaConversation.active?.turns.filter(\.mine).map(\.text).joined(separator: " ") ?? ""
        dismiss()
        startScan(said)
    }
}

// MARK: - Talking with Lea while scanning

/// Lea over the camera while scanning: the same conversation as this scan's report (it carries on there), one line
/// of captions, an ear that turns continuous hands-free on and off, and the mic (hold to ask, tap to talk
/// hands-free). Swipe up on it to start talking hands-free. Each turn tells Lea what the scan screen shows now
/// (`live`), so she can coach while you scan. Small, so the camera stays clear.
struct ScanTalkBar: View {
    @State private var convo: LeaConversation
    @State private var wake = WakeListener.shared
    @State private var pressedAt: Date?
    /// A tap instead of a hold: the caption says how to talk for a moment.
    @State private var tapHint = false

    init(scanID: String, gateway: GatewayClient, diag: Diag?, sessionID: @escaping () -> String?,
         live: @escaping () -> [String: String], eyes: LeaEyes? = nil) {
        let c = LeaConversation(scanID: scanID, gateway: gateway, diag: diag)
        c.sessionID = sessionID
        c.live = live
        c.eyes = eyes
        _convo = State(initialValue: c)
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
            // Lea's mark is the hold-to-talk target over the camera (her state, never fake movement).
            LeaMarkView(state: markState, size: 24, tint: Palette.next)
                .padding(10)
                .background(Circle().fill(pressedAt != nil ? Palette.next.opacity(0.22) : .clear))
                .contentShape(Circle())
                .holdOrTapToTalk(convo, pressedAt: $pressedAt) {   // a tap says how; the switch turns hands-free on
                    withAnimation(Motion.fade) { tapHint = true }
                    Task { try? await Task.sleep(for: .seconds(1.6)); withAnimation(Motion.fade) { tapHint = false } }
                }
                .accessibilityLabel("Talk to Lea")
                .accessibilityHint("Press and hold, ask, then let go")
                .accessibilityAddTraits(.isButton)
        }
        .padding(.leading, Spacing.xs)
        .padding(.trailing, Spacing.xxs)
        .padding(.vertical, Spacing.xxs)
        .background { SmokeFill(shape: Capsule()) }
        .dynamicTypeSize(...DynamicTypeSize.accessibility1)
        .contentShape(Capsule())
        .gesture(DragGesture(minimumDistance: 20).onEnded { if $0.translation.height < -30 { talkHandsFree() } })
        .environment(\.colorScheme, .dark)
        .onAppear {
            convo.appeared()
            if wake.continuous { wake.listenForFollowUp() }   // hands-free carries on into the scan
        }
        .task { await convo.loadHistory() }
        .onDisappear {
            convo.disappeared()
            if convo.visible == 0 { convo.stopLea() }
        }
    }

    private var listening: Bool { convo.phase == .listening || wake.mode == .followUp }
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
        if tapHint { return "Hold to talk" }
        if case .failed(let m) = convo.phase { return m }
        if !convo.liveCaption.isEmpty { return convo.liveCaption }
        if convo.phase == .thinking { return "Thinking…" }
        if let last = convo.turns.last(where: { !$0.mine }), convo.phase == .speaking || !listening { return last.text }
        return listening ? "Lea is listening. Ask anything while you scan." : "Swipe up and talk, or hold the mic."
    }

    /// Swipe up or tap: Lea listens hands-free and keeps listening after each answer.
    private func talkHandsFree() {
        convo.handsFree = true
        wake.continuous = true
        wake.listenForFollowUp()
    }
}

// MARK: - Hold to ask, tap for hands-free

extension View {
    /// The talk button's gesture (the conversation panel and the scan bar): pressing stops Lea at once (barge-in) and
    /// records; letting go after a hold sends the question; a tap (under 0.35 s) drops the recording and runs `tap`.
    func holdOrTapToTalk(_ convo: LeaConversation, pressedAt: Binding<Date?>, tap: @escaping () -> Void) -> some View {
        gesture(DragGesture(minimumDistance: 0)
            .onChanged { _ in
                guard pressedAt.wrappedValue == nil else { return }
                pressedAt.wrappedValue = Date()
                convo.pressToTalk()
            }
            .onEnded { _ in
                let held = pressedAt.wrappedValue.map { Date().timeIntervalSince($0) } ?? 0
                pressedAt.wrappedValue = nil
                if held < 0.35 { convo.cancelHold(); tap() } else { convo.releaseToTalk() }
            })
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
