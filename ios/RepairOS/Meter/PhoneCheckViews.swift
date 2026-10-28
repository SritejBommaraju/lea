@preconcurrency import AVFoundation
import SwiftUI
import UIKit

// One section per instrument of the "Check it with your phone" sheet. Each starts its sensor only when the person taps
// it, says what to do in a sentence or two, shows a live number, and sends the reading through `PhoneCheckEnv.post`.

/// What every instrument needs to send a reading, so the sections do not know about the gateway.
struct PhoneCheckEnv {
    /// The plan step a reading answers, if it does (see `MeterReadingContext.answeringActionID(for:)`).
    var actionID: (ReadingSubject) -> String?
    var scanID: String
    var sessionID: String
    /// Sends the readings; true when the gateway accepted all of them (the sheet shows the error otherwise).
    var post: @MainActor ([MeterReading]) async -> Bool
}

// MARK: - Camera preview

/// The camera's picture with an outline on the spot the tracker locked on. Capture-device coordinates (0 to 1 across
/// the unrotated sensor) are what the layer converts, so the outline stays on the LED however the phone is held.
final class CameraPreviewView: UIView {
    override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }

    private let ring = CAShapeLayer()
    var marker: CGPoint? { didSet { placeMarker() } }

    private var previewLayer: AVCaptureVideoPreviewLayer? { layer as? AVCaptureVideoPreviewLayer }

    override init(frame: CGRect) {
        super.init(frame: frame)
        ring.fillColor = nil
        ring.strokeColor = UIColor.systemYellow.cgColor
        ring.lineWidth = 3
        ring.path = UIBezierPath(ovalIn: CGRect(x: -18, y: -18, width: 36, height: 36)).cgPath
        ring.isHidden = true
        layer.addSublayer(ring)
        isAccessibilityElement = false
    }

    required init?(coder: NSCoder) { nil }

    override func layoutSubviews() {
        super.layoutSubviews()
        placeMarker()
    }

    func attach(_ session: AVCaptureSession) {
        previewLayer?.session = session
        previewLayer?.videoGravity = .resizeAspectFill
    }

    private func placeMarker() {
        guard let marker, let previewLayer else {
            ring.isHidden = true
            return
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        ring.position = previewLayer.layerPointConverted(fromCaptureDevicePoint: marker)
        ring.isHidden = false
        CATransaction.commit()
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    var marker: (x: Double, y: Double)?

    func makeUIView(context: Context) -> CameraPreviewView {
        let v = CameraPreviewView()
        v.attach(session)
        return v
    }

    func updateUIView(_ v: CameraPreviewView, context: Context) {
        v.marker = marker.map { CGPoint(x: $0.x, y: $0.y) }
    }
}

private struct PreviewFrame: View {
    let session: AVCaptureSession
    var marker: (x: Double, y: Double)?

    var body: some View {
        CameraPreview(session: session, marker: marker)
            .frame(height: 240)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .accessibilityHidden(true)
    }
}

// MARK: - Shared pieces

private struct SendButton: View {
    let title: String
    let sent: Bool
    let enabled: Bool
    let action: () async -> Void
    @State private var busy = false

    var body: some View {
        Button(sent ? "Sent" : title) {
            Task {
                busy = true
                await action()
                busy = false
            }
        }
        .disabled(!enabled || busy || sent)
    }
}

private struct Hint: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.footnote).foregroundStyle(Palette.secondary) }
}

private struct ProblemLine: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View { Text(text).font(.footnote).foregroundStyle(Palette.danger) }
}

// MARK: - The product's self-test

/// One check as the product (or its LED) reported it.
struct SelfTestRowView: View {
    let net: String
    let measured: String?
    let expected: String?
    let verdict: SelfTestFrame.Outcome?
    let note: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(net).font(.body.weight(.semibold))
                Spacer(minLength: 8)
                badge
            }
            let detail = [measured.map { "Measured \($0)." }, expected.map { "Should be \($0)." }, note]
                .compactMap { $0 }.joined(separator: " ")
            if !detail.isEmpty { Text(detail).font(.footnote).foregroundStyle(Palette.secondary) }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder private var badge: some View {
        switch verdict {
        case .ok?: Label("OK", systemImage: "checkmark.circle.fill").foregroundStyle(Palette.good)
        case .fail?: Label("Fail", systemImage: "xmark.octagon.fill").foregroundStyle(Palette.danger)
        case .unknown?: Label("Unknown", systemImage: "questionmark.circle").foregroundStyle(Palette.secondary)
        case nil: Label("Not reported", systemImage: "minus.circle").foregroundStyle(Palette.secondary)
        }
    }
}

/// The product checks itself and reports over Bluetooth.
struct SelfTestBluetoothSection: View {
    let plan: SelfTest
    let env: PhoneCheckEnv
    @StateObject private var link = MeterLink()
    @State private var sent = false

    var body: some View {
        Group {
            if let config = SelfTestLinkConfig(plan: plan) {
                content(config)
            } else {
                Hint("This product does not offer a Bluetooth self-test.")
            }
        }
        .onDisappear { link.stop() }
    }

    @ViewBuilder private func content(_ config: SelfTestLinkConfig) -> some View {
        Hint("Switch the product on and hold it close to the phone. It checks itself and tells the phone what it found.")
        switch link.phase {
        case .scanning, .connecting:
            HStack(spacing: 12) {
                ProgressView()
                Text(link.phase == .scanning ? "Looking for the product\u{2026}" : "Connecting\u{2026}")
            }
        case .running:
            HStack(spacing: 12) {
                ProgressView()
                Text("The product is checking itself\u{2026}")
            }
        case .off, .lost, .done:
            Button(link.frame == nil ? "Start the self-test" : "Run it again") {
                sent = false
                link.start(config)
            }
            if let m = link.message { ProblemLine(m) }
        }
        if let name = link.deviceName, link.phase != .off { Hint("Talking to \(name).") }
        if let frame = link.frame { results(frame) }
    }

    @ViewBuilder private func results(_ frame: SelfTestFrame) -> some View {
        let built = SelfTestRows.rows(frame: frame, plan: plan)
        ForEach(built.rows) { row in
            SelfTestRowView(net: row.net, measured: row.measured, expected: row.expected, verdict: row.outcome,
                            note: row.state == .kindMismatch ? "The product reported this check as another kind than the design lists, so it is not sent." : nil)
        }
        if !built.unlisted.isEmpty {
            Hint("The product also reported \(built.unlisted.count) check\(built.unlisted.count == 1 ? "" : "s") the design does not list. They are not sent.")
        }
        let readings = SelfTestRows.readings(rows: built.rows, transport: .ble, actionID: { env.actionID(.net($0)) },
                                             scanID: env.scanID, sessionID: env.sessionID)
        SendButton(title: "Send \(readings.count) result\(readings.count == 1 ? "" : "s")", sent: sent, enabled: !readings.isEmpty) {
            sent = await env.post(readings)
        }
    }
}

/// The product's status light blinks its result; the camera reads it.
struct SelfTestBlinkSection: View {
    let plan: SelfTest
    let env: PhoneCheckEnv
    @StateObject private var check: BlinkCheck
    @State private var sent = false

    init(plan: SelfTest, code: BlinkCode, env: PhoneCheckEnv) {
        self.plan = plan
        self.env = env
        _check = StateObject(wrappedValue: BlinkCheck(code: code, checkCount: plan.checks.count))
    }

    var body: some View {
        Group {
            Hint("Switch the product on so its status light blinks its result, then hold the phone steady with the light in view. This works with any product with that light: no Bluetooth needed.")
            if check.phase != .idle, check.phase != .complete {
                PreviewFrame(session: check.session, marker: check.analysis.marker)
            }
            switch check.phase {
            case .idle, .failed:
                Button("Read the status light") {
                    sent = false
                    check.start()
                }
            case .starting, .watching:
                Button("Stop") { check.stop() }
            case .complete: EmptyView()
            }
            progress
            if check.phase == .complete, case .complete(let outcomes) = check.analysis.progress { results(outcomes) }
        }
        .onDisappear { check.stop() }
    }

    @ViewBuilder private var progress: some View {
        let total = plan.checks.count
        if case .failed(let m) = check.phase {
            ProblemLine(m)
        } else if check.phase != .idle {
            if case .reading(let done) = check.analysis.progress {
                ProgressView(value: Double(min(done.count, total)), total: Double(max(total, 1)))
                Text("Check \(min(done.count + 1, total)) of \(total)").font(.subheadline.weight(.semibold))
            }
            Hint(check.status)
            if check.phase == .watching, check.fps > 0, check.fps < 50 {
                ProblemLine("This phone's camera runs at \(Int(check.fps)) frames a second here, so short flashes may be missed.")
            }
        }
    }

    @ViewBuilder private func results(_ outcomes: [SelfTestFrame.Outcome]) -> some View {
        let built = SelfTestRows.rows(blink: outcomes, plan: plan)
        if let problem = built.problem {
            ProblemLine(problem)
        } else {
            ForEach(built.rows) { row in
                SelfTestRowView(net: row.net, measured: nil, expected: row.expected, verdict: row.outcome, note: nil)
            }
            let readings = SelfTestRows.readings(blink: built.rows, actionID: { env.actionID(.net($0)) }, scanID: env.scanID,
                                                 sessionID: env.sessionID)
            SendButton(title: "Send \(readings.count) result\(readings.count == 1 ? "" : "s")", sent: sent, enabled: !readings.isEmpty) {
                sent = await env.post(readings)
            }
        }
        Button("Read it again") {
            sent = false
            check.start()
        }
    }
}

// MARK: - The phone's own sensors

/// Which part the reading is about: the design's own name for it (for example M1 or LED1).
struct PartField: View {
    @Binding var part: String

    var body: some View {
        TextField("Which part? (for example M1)", text: $part)
            .textInputAutocapitalization(.characters)
            .autocorrectionDisabled()
    }
}

private func hasPart(_ part: String) -> Bool { !part.trimmingCharacters(in: .whitespaces).isEmpty }

struct MagnetometerSection: View {
    let env: PhoneCheckEnv
    let part: String
    @StateObject private var check = MagnetometerCheck()
    @State private var sent = false

    var body: some View {
        Group {
            Hint("Keep the product switched off and hold the top edge of the phone against the motor. Tap Start, wait two seconds while the phone measures the background, then switch the product on.")
            switch check.phase {
            case .idle, .failed:
                Button("Start") {
                    sent = false
                    check.start()
                }
                if case .failed(let m) = check.phase { ProblemLine(m) }
            case .baseline:
                ProgressView(value: check.progress)
                Text("Measuring the background\u{2026} keep the product off.")
            case .live:
                live
                Button("Stop") { check.stop() }
            }
        }
        .onDisappear { check.stop() }
    }

    @ViewBuilder private var live: some View {
        Text("Switch the product on now.").font(.subheadline.weight(.semibold))
        if let d = check.delta, let b = check.baseline, let state = check.state {
            Text(String(format: "%.1f \u{B5}T change", d)).font(.title2.monospacedDigit().weight(.semibold))
                .accessibilityLabel(String(format: "Field change %.1f microtesla", d))
            Text(state == .current ? "Current detected." : "No current detected.").font(.body.weight(.semibold))
            Hint(String(format: "It counts as current from %.1f \u{B5}T: 5, or three times the phone's own noise (%.2f) if that is larger.", b.threshold, b.noise))
            SendButton(title: "Send this reading", sent: sent, enabled: hasPart(part)) {
                let value = Value.magnetometer(deltaUT: PhoneReadings.rounded(d, places: 1), state: state)
                sent = await env.post([PhoneReadings.reading(kind: .magnetometer, part: part, value: value, actionID: env.actionID(.part(part, .magnetometer)),
                                                              scanID: env.scanID, sessionID: env.sessionID)])
            }
            if !hasPart(part) { Hint("Name the part above to send.") }
        }
    }
}

struct AudioSection: View {
    let env: PhoneCheckEnv
    let part: String
    @StateObject private var check = AudioCheck()
    @State private var sent = false

    var body: some View {
        Group {
            Hint("Hold the phone's microphone near the motor or fan while it runs. The phone listens for its main tone.")
            switch check.phase {
            case .idle, .failed:
                Button("Listen") {
                    sent = false
                    check.start()
                }
                if case .failed(let m) = check.phase { ProblemLine(m) }
            case .starting:
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Starting the microphone\u{2026}")
                }
            case .listening:
                listening
                Button("Stop") { check.stop() }
            }
        }
        .onDisappear { check.stop() }
    }

    @ViewBuilder private var listening: some View {
        if let tone = check.tone {
            Text(String(format: "%.1f Hz", tone.hz)).font(.title2.monospacedDigit().weight(.semibold))
                .accessibilityLabel(String(format: "Main tone %.1f hertz", tone.hz))
        } else {
            Text("No clear tone yet").font(.title2.weight(.semibold))
        }
        Hint(String(format: "Level %.0f dB. A steady tone is needed to send.", Double(check.levelDB)))
        if let hz = check.steady {
            SendButton(title: String(format: "Send %.1f Hz", hz), sent: sent, enabled: hasPart(part)) {
                let value = Value.audio(hz: PhoneReadings.rounded(hz, places: 1))
                sent = await env.post([PhoneReadings.reading(kind: .audio, part: part, value: value, actionID: env.actionID(.part(part, .audio)),
                                                              scanID: env.scanID, sessionID: env.sessionID)])
            }
            if !hasPart(part) { Hint("Name the part above to send.") }
        }
    }
}

struct FlickerSection: View {
    let env: PhoneCheckEnv
    let part: String
    @StateObject private var check = FlickerCheck()
    @State private var sent = false

    var body: some View {
        Group {
            Hint("Point the camera at the light and hold still for two seconds. The phone uses the fastest frame rate its camera offers.")
            if check.phase == .measuring {
                PreviewFrame(session: check.session, marker: nil)
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Measuring\u{2026}")
                }
            } else {
                Button(check.result == nil ? "Measure flicker" : "Measure again") {
                    sent = false
                    check.measure()
                }
            }
            if case .failed(let m) = check.phase { ProblemLine(m) }
            if let r = check.result { result(r) }
        }
        .onDisappear { check.stop() }
    }

    @ViewBuilder private func result(_ r: FlickerAnalysis.Result) -> some View {
        if r.hz > 0 {
            Text(String(format: "%.1f Hz, %.0f %% deep", r.hz, r.depth * 100)).font(.title2.monospacedDigit().weight(.semibold))
        } else {
            Text("No flicker seen").font(.title2.weight(.semibold))
        }
        Hint(String(format: "The camera ran at %.0f frames a second, so it can only show flicker up to %.0f Hz. Faster flicker, such as most LED dimming, shows as steady or at a false lower rate.", r.rate, r.limitHz))
        SendButton(title: "Send this reading", sent: sent, enabled: hasPart(part)) {
            let value = Value.flicker(hz: PhoneReadings.rounded(r.hz, places: 1), depth: PhoneReadings.rounded(r.depth, places: 2))
            sent = await env.post([PhoneReadings.reading(kind: .flicker, part: part, value: value, actionID: env.actionID(.part(part, .flicker)),
                                                          scanID: env.scanID, sessionID: env.sessionID)])
        }
        if !hasPart(part) { Hint("Name the part above to send.") }
    }
}

struct LightSection: View {
    let env: PhoneCheckEnv
    let part: String
    @StateObject private var check = LightCheck()
    @State private var sent = false

    var body: some View {
        Group {
            Hint("This is relative: iOS has no light meter, so the phone compares the camera's exposure with a baseline you take first. Point the camera at the part both times, from the same spot.")
            if check.phase == .measuring {
                PreviewFrame(session: check.session, marker: nil)
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Measuring\u{2026}")
                }
            } else {
                Button(check.baseline == nil ? "1. Product off: take the baseline" : "1. Take the baseline again") {
                    sent = false
                    check.takeBaseline()
                }
                if check.baseline != nil {
                    Button("2. Switch the product on, then measure") {
                        sent = false
                        check.measure()
                    }
                }
            }
            if case .failed(let m) = check.phase { ProblemLine(m) }
            if let state = check.state, let ratio = check.ratio {
                Text(Self.words(state)).font(.title2.weight(.semibold))
                Hint(String(format: "About %.1f times the baseline. Off is under %.1f times, dim is under %.0f times, on is more.",
                            ratio, RelativeLight.unchangedBelow, RelativeLight.dimBelow))
                SendButton(title: "Send this reading", sent: sent, enabled: hasPart(part)) {
                    sent = await env.post([PhoneReadings.reading(kind: .light, part: part, value: .light(state: state),
                                                                  actionID: env.actionID(.part(part, .light)), scanID: env.scanID,
                                                                  sessionID: env.sessionID)])
                }
                if !hasPart(part) { Hint("Name the part above to send.") }
            }
        }
        .onDisappear { check.stop() }
    }

    static func words(_ state: ValueState) -> String {
        switch state {
        case .on: "Light is on"
        case .dim: "Light is dim"
        default: "Light is off"
        }
    }
}

struct LedStateSection: View {
    let env: PhoneCheckEnv
    let part: String
    @StateObject private var check = LedStateCheck()
    @State private var sent = false

    var body: some View {
        Group {
            Hint("Point the camera at the LED and hold still for three seconds. The phone tells lit, dark and blinking apart, and notes the colour. It is the camera's judgement of the picture.")
            if check.phase == .watching {
                PreviewFrame(session: check.session, marker: nil)
                HStack(spacing: 12) {
                    ProgressView()
                    Text("Watching\u{2026}")
                }
            } else {
                Button(check.result == nil ? "Look at the LED" : "Look again") {
                    sent = false
                    check.watch()
                }
            }
            if case .failed(let m) = check.phase { ProblemLine(m) }
            if let r = check.result {
                Text(Self.words(r)).font(.title2.weight(.semibold))
                SendButton(title: "Send this reading", sent: sent, enabled: hasPart(part)) {
                    sent = await env.post([PhoneReadings.reading(kind: .led, part: part, value: .led(state: r.state, color: r.color),
                                                                  actionID: env.actionID(.part(part, .led)), scanID: env.scanID,
                                                                  sessionID: env.sessionID)])
                }
                if !hasPart(part) { Hint("Name the part above to send.") }
            }
        }
        .onDisappear { check.stop() }
    }

    static func words(_ r: LedObservation.Result) -> String {
        let colour = r.color.map { " (\($0))" } ?? ""
        switch r.state {
        case .blinking: return "Blinking" + colour
        case .on: return "Lit" + colour
        default: return "Dark"
        }
    }
}
