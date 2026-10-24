import SwiftUI

/// Voice mode: a hands-free conversation with Lea. Keep explaining what you think is wrong; each answer is followed by
/// listening again, and anything easier to type can be typed. The orb shows whether Lea is listening, thinking or
/// speaking; when Lea asks you to scan the object, a scan button appears and leads straight into the scan.
struct VoiceModeView: View {
    @ObservedObject var voice: VoiceCoordinator
    let scanOffered: Bool
    let startScan: () -> Void
    let close: () -> Void
    @State private var pressing = false
    @State private var typed = ""
    @FocusState private var typing: Bool

    var body: some View {
        VStack(spacing: Spacing.l) {
            HStack {
                Text(status).font(.pillLabel).foregroundStyle(.secondary)
                Spacer()
                Button(action: close) { Image(systemName: Symbol.close) }
                    .buttonStyle(RoundIconButtonStyle())
                    .accessibilityLabel("Close voice mode")
            }
            if voice.exchanges.isEmpty {
                Spacer()
                Orb(state: voice.state).frame(width: 220, height: 220).accessibilityHidden(true)
                Text(voice.partial.isEmpty ? "Tell Lea what's going on and what you think is wrong." : voice.partial)
                    .font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                Spacer()
            } else {
                Orb(state: voice.state).frame(width: 72, height: 72).accessibilityHidden(true)
                conversation
            }
            if scanOffered {
                Button(action: startScan) { Label("Scan it", systemImage: Symbol.scan) }
                    .buttonStyle(.primary)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
            controls
        }
        .padding(Spacing.gutter)
        .background(Palette.canvas.ignoresSafeArea())
        .animation(Motion.settle, value: scanOffered)
        .animation(Motion.standard, value: voice.exchanges)
        .sensoryFeedback(Haptic.milestone, trigger: scanOffered) { _, offered in offered }
    }

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: Spacing.m) {
                    ForEach(voice.exchanges) { e in
                        Bubble(text: e.said, mine: true)
                        if let reply = e.reply { Bubble(text: reply, mine: false) }
                    }
                    if voice.state == .listening, !voice.partial.isEmpty { Bubble(text: voice.partial, mine: true).opacity(0.6) }
                    Color.clear.frame(height: 1).id("end")
                }
            }
            .scrollDismissesKeyboard(.interactively)
            .onChange(of: voice.exchanges) { withAnimation(Motion.standard) { proxy.scrollTo("end", anchor: .bottom) } }
            .onChange(of: voice.partial) { proxy.scrollTo("end", anchor: .bottom) }
        }
    }

    private var controls: some View {
        HStack(spacing: Spacing.m) {
            FieldWell(symbol: "keyboard") {
                TextField("Type to Lea", text: $typed, axis: .vertical)
                    .lineLimit(1...4)
                    .focused($typing)
                    .submitLabel(.send)
                    .onSubmit(send)
            }
            if typing || !typed.isEmpty {
                Button(action: send) { Image(systemName: "arrow.up.circle.fill").font(.system(size: 34)) }
                    .disabled(typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityLabel("Send")
            } else {
                Toggle(isOn: Binding(get: { voice.handsFree }, set: { voice.setHandsFree($0) })) {
                    Image(systemName: "waveform")
                }
                .toggleStyle(.button)
                .accessibilityLabel("Hands-free")
                Image(systemName: pressing ? "mic.circle.fill" : "mic.circle")
                    .font(.system(size: 44))
                    .foregroundStyle(pressing ? Palette.danger : Palette.accent)
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { _ in if !pressing { pressing = true; voice.pushToTalkBegan() } }
                        .onEnded { _ in pressing = false; voice.pushToTalkEnded() })
                    .accessibilityLabel("Hold to talk to Lea")
            }
        }
    }

    private func send() {
        let text = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        typed = ""
        voice.send(typed: text)
    }

    private var status: String {
        switch voice.state {
        case .idle: voice.handsFree ? "Hands-free" : "Hold the mic to talk"
        case .listening: "Listening"
        case .thinking: "Thinking"
        case .speaking: "Lea"
        }
    }
}

/// What you said sits on the right; Lea's answers on the left.
private struct Bubble: View {
    let text: String
    let mine: Bool

    var body: some View {
        HStack {
            if mine { Spacer(minLength: Spacing.xxl) }
            Text(text)
                .font(mine ? .callout : .body)
                .padding(.horizontal, Spacing.m)
                .padding(.vertical, Spacing.s)
                .background(mine ? Palette.accent.opacity(0.14) : Palette.surface,
                            in: RoundedRectangle(cornerRadius: Radius.medium, style: .continuous))
            if !mine { Spacer(minLength: Spacing.xxl) }
        }
    }
}

/// Breathes while listening, pulses while speaking, turns while thinking, rests when idle.
private struct Orb: View {
    let state: VoiceCoordinator.State

    var body: some View {
        TimelineView(.animation) { context in
            let t = context.date.timeIntervalSinceReferenceDate
            let scale: Double = switch state {
            case .idle: 0.92
            case .listening: 1 + 0.04 * sin(t * 2.2)
            case .thinking: 0.96
            case .speaking: 1 + 0.08 * abs(sin(t * 6))
            }
            Circle()
                .fill(AngularGradient(colors: [Palette.accent, Palette.accentFill.opacity(0.55), Palette.cameraAccent, Palette.accent],
                                      center: .center, angle: .degrees(state == .thinking ? t * 120 : t * 12)))
                .blur(radius: 6)
                .overlay(Circle().strokeBorder(Palette.accent.opacity(0.25), lineWidth: 2))
                .scaleEffect(scale)
                .shadow(color: Palette.accent.opacity(state == .speaking ? 0.5 : 0.25), radius: 30)
        }
    }
}
