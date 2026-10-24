import Combine
import SwiftUI

/// VEL looks at the screen before it teaches; Lea looks at the camera before you take a photo. Every ~1.5 s a frame
/// goes to the bridge's Jev look (usable? what's wrong? what part? where? same as the reference? damaged-looking?)
/// and the tip is shown and, if asked, spoken. Frames are guidance only: nothing here becomes evidence.
@MainActor
final class CaptureCoach: ObservableObject {
    @Published var on = false { didSet { on ? start() : stop() } }
    @Published var speakTips = true
    @Published private(set) var seen: LookResult?
    @Published private(set) var frameSize: CGSize = .zero
    @Published private(set) var status = ""

    var subject = ""
    @Published var referenceAssetId: String?
    @Published var referenceSubject: String?
    weak var camera: CameraService?
    weak var sync: SessionSync?
    weak var voice: VoiceCoordinator?

    private var loop: Task<Void, Never>?
    private var lastSpoken = ""
    private var lastSpokenAt = Date.distantPast

    private func start() {
        loop?.cancel()
        guard sync?.health?.vision.jevConfigured == true else {
            status = "Coaching needs a Jev key on the bridge."
            on = false
            return
        }
        status = "Looking…"
        loop = Task { [weak self] in
            while !Task.isCancelled {
                await self?.tick()
                try? await Task.sleep(for: .milliseconds(1500))
            }
        }
    }

    private func stop() {
        loop?.cancel()
        loop = nil
        seen = nil
        status = ""
    }

    private func tick() async {
        guard let camera, let client = sync?.client, let remote = sync?.remoteId,
              let frame = await camera.liveFrame() else { return }
        do {
            let result = try await client.look(sessionId: remote, frame: frame.jpeg, subject: subject, referenceAssetId: referenceAssetId)
            seen = result
            frameSize = frame.size
            status = result.tip.isEmpty ? "" : result.tip
            // Speak a tip only when it changes, and not more than every 6 s (VEL keeps asides rare).
            if speakTips, !result.tip.isEmpty, result.tip != lastSpoken || Date().timeIntervalSince(lastSpokenAt) > 20,
               Date().timeIntervalSince(lastSpokenAt) > 6 {
                lastSpoken = result.tip
                lastSpokenAt = Date()
                voice?.sayAside(result.tip)
            }
        } catch {
            status = error.localizedDescription
        }
    }
}

/// The part Jev pointed at, drawn over the aspect-filled camera preview.
struct CoachOverlay: View {
    let seen: LookResult?
    let frameSize: CGSize

    var body: some View {
        GeometryReader { geo in
            if let box = seen?.box, box.confidence >= 0.4, frameSize.width > 0 {
                let scale = max(geo.size.width / frameSize.width, geo.size.height / frameSize.height)
                let ox = (geo.size.width - frameSize.width * scale) / 2, oy = (geo.size.height - frameSize.height * scale) / 2
                let rect = CGRect(x: box.left * frameSize.width * scale + ox, y: box.top * frameSize.height * scale + oy,
                                  width: (box.right - box.left) * frameSize.width * scale, height: (box.bottom - box.top) * frameSize.height * scale)
                RoundedRectangle(cornerRadius: 8)
                    .stroke((seen?.looksDamaged ?? 0) >= 0.6 ? Color.orange : Color.mint, lineWidth: 3)
                    .frame(width: rect.width, height: rect.height)
                    .position(x: rect.midX, y: rect.midY)
                if let part = seen?.part {
                    Text(part.replacingOccurrences(of: "_", with: " "))
                        .font(.caption2.weight(.bold)).padding(4).background(.black.opacity(0.6), in: Capsule())
                        .position(x: rect.minX + 40, y: max(12, rect.minY - 12))
                }
            }
        }
        .allowsHitTesting(false)
    }
}
