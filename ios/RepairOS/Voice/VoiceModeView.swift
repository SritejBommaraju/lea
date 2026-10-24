import SwiftUI

/// Voice mode: the whole screen is the conversation. An orb shows whether Lea is listening, thinking or speaking;
/// captions show what you said and Lea's answer; when Lea asks you to scan the object, a scan card appears and leads
/// straight into the scan.
struct VoiceModeView: View {
    @ObservedObject var voice: VoiceCoordinator
    let scanOffered: Bool
    let startScan: () -> Void
    let close: () -> Void
    @State private var pressing = false

    var body: some View {
        VStack(spacing: Spacing.xl) {
            HStack {
                Text(status).font(.pillLabel).foregroundStyle(.secondary)
                Spacer()
                Button(action: close) { Image(systemName: Symbol.close) }
                    .buttonStyle(RoundIconButtonStyle())
                    .accessibilityLabel("Close voice mode")
            }
            Spacer()
            Orb(state: voice.state)
                .frame(width: 220, height: 220)
                .accessibilityHidden(true)
            VStack(spacing: Spacing.m) {
                if let heard = voice.partial.isEmpty ? voice.lastHeard : voice.partial {
                    Text(heard).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                if let reply = voice.lastReply {
                    Text(reply).font(.sectionTitle).multilineTextAlignment(.center)
                }
            }
            .frame(maxWidth: .infinity)
            .animation(Motion.standard, value: voice.lastReply)
            Spacer()
            if scanOffered {
                Button(action: startScan) { Label("Scan it", systemImage: Symbol.scan) }
                    .buttonStyle(.primary)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
                Text("Or just say “okay”.").font(.footnote).foregroundStyle(.secondary)
            }
            HStack(spacing: Spacing.xl) {
                Toggle(isOn: Binding(get: { voice.handsFree }, set: { voice.setHandsFree($0) })) {
                    Label("Hands-free", systemImage: "waveform")
                }
                .toggleStyle(.button)
                Image(systemName: pressing ? "mic.circle.fill" : "mic.circle")
                    .font(.system(size: 64))
                    .foregroundStyle(pressing ? Palette.danger : Palette.accent)
                    .gesture(DragGesture(minimumDistance: 0)
                        .onChanged { _ in if !pressing { pressing = true; voice.pushToTalkBegan() } }
                        .onEnded { _ in pressing = false; voice.pushToTalkEnded() })
                    .accessibilityLabel("Hold to talk to Lea")
            }
        }
        .padding(Spacing.gutter)
        .background(Palette.canvas.ignoresSafeArea())
        .animation(Motion.settle, value: scanOffered)
        .sensoryFeedback(Haptic.milestone, trigger: scanOffered) { _, offered in offered }
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
