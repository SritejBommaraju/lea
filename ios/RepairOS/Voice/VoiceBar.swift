import SwiftUI

/// Hold the mic to talk, or turn on hands-free and just talk (you can interrupt); "Hey Leah" appears once its model
/// is installed. Partial words show while you speak; Lea's last reply shows after.
struct VoiceBar: View {
    @ObservedObject var voice: VoiceCoordinator
    @State private var pressing = false

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.subheadline.weight(.semibold))
                Text(voice.partial.isEmpty ? (voice.lastReply ?? subtitle) : voice.partial)
                    .font(.caption2).foregroundStyle(.secondary).lineLimit(2)
            }
            Spacer()
            Toggle(isOn: Binding(get: { voice.handsFree }, set: { voice.setHandsFree($0) })) { Text("Hands-free").font(.caption2) }
                .toggleStyle(.button)
            if voice.wakeAvailable {
                Toggle(isOn: Binding(get: { voice.wakeOn }, set: { voice.setWake($0) })) { Text("Hey Leah").font(.caption2) }
                    .toggleStyle(.button)
            }
            Image(systemName: pressing ? "mic.circle.fill" : "mic.circle")
                .font(.system(size: 38))
                .foregroundStyle(pressing ? Color.red : Color.accentColor)
                .gesture(DragGesture(minimumDistance: 0)
                    .onChanged { _ in if !pressing { pressing = true; voice.pushToTalkBegan() } }
                    .onEnded { _ in pressing = false; voice.pushToTalkEnded() })
                .accessibilityLabel("Hold to talk to Lea")
        }
        .padding(12)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private var title: String {
        switch voice.state {
        case .idle: "Talk to Lea"
        case .listening: voice.handsFree ? "Listening (hands-free)" : "Listening"
        case .thinking: "Thinking"
        case .speaking: "Lea is speaking"
        }
    }

    private var subtitle: String {
        "\(voice.handsFree ? "Just talk; you can interrupt" : "Hold the mic to talk") · \(voice.listenerName)"
    }
}
