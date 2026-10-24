import SwiftUI

/// Lea during a scan or on its report: a slim bar over the camera that shows whether Lea is listening, thinking or
/// speaking, the words being heard or Lea's last answer, a hands-free switch and hold-to-talk. Small enough to leave
/// the camera view alone.
struct LiveVoiceBar: View {
    @ObservedObject var voice: VoiceCoordinator
    @State private var pressing = false

    var body: some View {
        HStack(spacing: Spacing.s) {
            VoiceOrb(state: voice.state).frame(width: 26, height: 26).accessibilityHidden(true)
            Text(caption)
                .font(.footnote.weight(.medium))
                .foregroundStyle(.white)
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .animation(Motion.fade, value: caption)
            Button { voice.setHandsFree(!voice.handsFree) } label: {
                Image(systemName: voice.handsFree ? "waveform.circle.fill" : "waveform.circle")
                    .font(.system(size: 26))
                    .foregroundStyle(voice.handsFree ? Palette.cameraAccent : .white)
            }
            .accessibilityLabel(voice.handsFree ? "Hands-free on" : "Hands-free off")
            Image(systemName: pressing ? "mic.circle.fill" : "mic.circle")
                .font(.system(size: 30))
                .foregroundStyle(pressing ? Palette.danger : .white)
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { _ in if !pressing { pressing = true; voice.pushToTalkBegan() } }
                    .onEnded { _ in pressing = false; voice.pushToTalkEnded() })
                .accessibilityLabel("Hold to talk to Lea")
        }
        .padding(.horizontal, Spacing.m)
        .padding(.vertical, Spacing.s)
        .background(.black.opacity(0.55), in: Capsule())
        .environment(\.colorScheme, .dark)
    }

    private var caption: String {
        if voice.state == .listening, !voice.partial.isEmpty { return voice.partial }
        switch voice.state {
        case .thinking: return "Thinking…"
        case .speaking: return voice.lastReply ?? "Lea"
        default:
            if let reply = voice.lastReply { return reply }
            return voice.handsFree ? "Lea is listening. Ask anything while you scan." : "Hold the mic to ask Lea while you scan."
        }
    }
}
