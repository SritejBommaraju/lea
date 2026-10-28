import SwiftUI

/// What a meter reading is about, so the sheet can open already filled in. Built from a `NextAction` (kind, net, pins,
/// the action it answers) or from the report's next step (kind, action, and the net and probe points when the design
/// engine named them).
struct MeterReadingContext: Identifiable {
    let id = UUID()
    var actionID: String?
    var kind: MeterReadingKind
    var net: String
    var pins: [String]
    /// What to watch out for before probing, shown above the probe points (the report's `next_step.safety_notes`).
    var safetyNotes: [String]
    /// The design engine's own words for the check (`next_step.instruction`).
    var instruction: String?
    /// What a good reading looks like (`next_step.expected`), said before measuring and beside the verdict.
    var expected: ExpectedReading?

    init(kind: MeterReadingKind = .continuity, net: String = "", pins: [String] = [], actionID: String? = nil,
         safetyNotes: [String] = [], instruction: String? = nil, expected: ExpectedReading? = nil) {
        self.kind = kind
        self.net = net
        self.pins = pins
        self.actionID = actionID
        self.safetyNotes = safetyNotes
        self.instruction = instruction
        self.expected = expected
    }

    /// A check the report named: the net, the two probe points and the check's id are known, so the sheet walks the
    /// customer through it and they only type what the meter shows (any meter, no Bluetooth needed).
    var isGuided: Bool { actionID != nil && !net.isEmpty && pins.count == 2 }

    init(action: NextAction) {
        self.init(kind: Self.kind(forAction: action.kind.rawValue) ?? .continuity, net: action.target?.net ?? "",
                  pins: action.target?.pins ?? [], actionID: action.actionID)
    }

    init(step: ReportModel.NextStep) {
        self.init(kind: Self.kind(forAction: step.kind) ?? .continuity, net: step.net ?? "", pins: step.pins,
                  actionID: step.actionID, safetyNotes: step.safetyNotes, instruction: step.instruction,
                  expected: step.expected)
    }

    static func kind(forAction kind: String) -> MeterReadingKind? {
        switch kind {
        case "run_continuity_test": .continuity
        case "run_resistance_test": .resistance
        case "run_voltage_test": .voltageDc
        default: nil
        }
    }
}

/// The rules of a meter reading, apart from the screen so they can be tested.
enum MeterReadingDraft {
    /// Continuity and resistance are read with the product off, voltage with it on (invariant S4).
    static func powerState(_ kind: MeterReadingKind) -> MeterReadingPowerState {
        kind == .voltageDc ? .on : .off
    }

    /// Shown before the probe points.
    static func banner(_ kind: MeterReadingKind) -> String {
        switch kind {
        case .continuity, .resistance: "Switch the product off and disconnect its power source and battery."
        case .voltageDc: "The product is powered: keep the probes apart."
        }
    }

    /// The two probe points, trimmed; nil when none were given.
    static func pins(_ a: String, _ b: String) -> [String]? {
        let list = [a, b].map { $0.trimmingCharacters(in: .whitespaces) }
        return list.allSatisfy(\.isEmpty) ? nil : list
    }

    /// Why this cannot be sent yet, in words, or nil.
    static func problem(kind: MeterReadingKind, net: String, pinA: String, pinB: String) -> String? {
        if net.trimmingCharacters(in: .whitespaces).isEmpty { return "Enter the net name." }
        let p = pins(pinA, pinB)
        if let p, p.contains(where: \.isEmpty) { return "Enter both probe points, or neither." }
        if p == nil, kind != .voltageDc { return "Enter the two probe points." }
        return nil
    }

    /// The value the meter's sample gives for this kind, or nil when it is for another mode or the meter flagged an error.
    static func value(from sample: MeterSample, kind: MeterReadingKind) -> Value? {
        guard !sample.error, sample.mode == MeterMode(kind: kind), sample.value.isFinite else { return nil }
        switch kind {
        case .continuity: return sample.continuity.map { Value(volts: nil, continuity: $0, ohms: nil) }
        case .voltageDc: return Value(volts: Double(sample.value), continuity: nil, ohms: nil)
        case .resistance: return sample.value >= 0 ? Value(volts: nil, continuity: nil, ohms: Double(sample.value)) : nil
        }
    }

    /// A typed value ("4.98", "4,98", "120") for this kind; nil when it is not a number the kind allows.
    static func value(typed text: String, kind: MeterReadingKind, beeps: Bool) -> Value? {
        if kind == .continuity { return Value(volts: nil, continuity: beeps, ohms: nil) }
        let t = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
        guard let x = Double(t), x.isFinite else { return nil }
        if kind == .voltageDc { return Value(volts: x, continuity: nil, ohms: nil) }
        return x >= 0 ? Value(volts: nil, continuity: nil, ohms: x) : nil
    }

    /// The `meter` object for a reading the app took over Bluetooth.
    static func meter(sample: MeterSample, model: String?, firmware: String?, batteryV: Double?, intervalMs: UInt32) -> Meter {
        Meter(batteryV: batteryV, firmware: firmware, intervalMS: Int(intervalMs), mode: sample.mode.contract, model: model,
              range: sample.autoRange ? (sample.rangeLabel.map { "auto (\($0))" } ?? "auto") : sample.rangeLabel,
              readAt: sample.readAt)
    }

    static func reading(context: MeterReadingContext, kind: MeterReadingKind, net: String, pinA: String, pinB: String,
                        scanID: String, sessionID: String, value: Value, meter: Meter?, rawText: String?,
                        now: Date = Date(), id: String = "rd_" + ScanStorage.hex(8)) -> MeterReading {
        MeterReading(actionID: context.actionID, condition: nil, expected: nil, kind: kind, meter: meter,
                     net: net.trimmingCharacters(in: .whitespaces), pins: pins(pinA, pinB), powerState: powerState(kind),
                     rawText: rawText, readingID: id, recordedAt: now, scanID: scanID, sessionID: sessionID,
                     source: meter == nil ? .typed : .ble, value: value, verdict: nil)
    }
}

/// Add one meter reading to the report: from a Bluetooth meter held to the phone, or typed. The power state is shown
/// before the probe points, and the reading goes to the gateway, which judges it (the phone never decides).
struct MeterReadingSheet: View {
    let context: MeterReadingContext
    let scanID: String
    let sessionID: String
    let gateway: GatewayClient?
    /// Called once the gateway has accepted the reading; the report then says it was sent.
    let sent: () -> Void

    @Environment(\.dismiss) private var dismiss
    @StateObject private var link = MeterLink()
    @State private var kind: MeterReadingKind
    @State private var net: String
    @State private var pinA: String
    @State private var pinB: String
    @State private var typed = ""
    @State private var beeps = true
    @State private var sending = false
    @State private var error: String?
    /// The reading the gateway has accepted, and what the design engine said about it.
    @State private var savedID: String?
    @State private var verdict: ReadingVerdict?
    @State private var slow = false

    init(context: MeterReadingContext, scanID: String, sessionID: String, gateway: GatewayClient?, sent: @escaping () -> Void) {
        self.context = context
        self.scanID = scanID
        self.sessionID = sessionID
        self.gateway = gateway
        self.sent = sent
        _kind = SwiftUI.State(initialValue: context.kind)
        _net = SwiftUI.State(initialValue: context.net)
        _pinA = SwiftUI.State(initialValue: context.pins.first ?? "")
        _pinB = SwiftUI.State(initialValue: context.pins.dropFirst().first ?? "")
    }

    var body: some View {
        NavigationStack {
            Form {
                if context.isGuided {
                    guidedCheck
                    typedSection
                    meterSection
                } else {
                    Section {
                        Picker("Measuring", selection: $kind) {
                            Text("Continuity").tag(MeterReadingKind.continuity)
                            Text("DC voltage").tag(MeterReadingKind.voltageDc)
                            Text("Resistance").tag(MeterReadingKind.resistance)
                        }
                        .pickerStyle(.segmented)
                    }
                    Section {
                        Label(MeterReadingDraft.banner(kind),
                              systemImage: kind == .voltageDc ? "bolt.fill" : "powerplug.fill")
                            .font(.body.weight(.semibold))
                            .accessibilityLabel("Power state: " + MeterReadingDraft.banner(kind))
                        ForEach(context.safetyNotes.indices, id: \.self) { i in
                            Label(context.safetyNotes[i], systemImage: "exclamationmark.triangle.fill")
                                .font(.subheadline).foregroundStyle(Palette.caution)
                        }
                    }
                    Section("Probe points") {
                        TextField("Net name", text: $net).textInputAutocapitalization(.characters).autocorrectionDisabled()
                        TextField(kind == .voltageDc ? "Probe + (for example J1.1)" : "First point (for example J1.1)", text: $pinA)
                            .textInputAutocapitalization(.characters).autocorrectionDisabled()
                        TextField(kind == .voltageDc ? "Probe - (for example J1.2)" : "Second point", text: $pinB)
                            .textInputAutocapitalization(.characters).autocorrectionDisabled()
                    }
                    meterSection
                    typedSection
                }
                verdictSection
                if let error {
                    Section { Text(error).foregroundStyle(Palette.danger) }
                }
            }
            // The form is read-only while a reading is on its way and after it is saved; the toolbar's Done stays live.
            .disabled(sending || savedID != nil)
            .navigationTitle("Meter reading")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(savedID == nil ? "Cancel" : "Done") { dismiss() }.fontWeight(savedID == nil ? .regular : .semibold)
                }
            }
            .onValueChange(of: kind) { _, new in
                if link.phase == .ready || link.phase == .measuring { link.start(kind: new) }
            }
            .onDisappear { link.stop() }
            .task(id: savedID) { await waitForVerdict() }
        }
    }

    // MARK: The check, in plain words (a check the report named)

    /// What to do, in order: the power state first, then the dial, the two places to touch, and what a good reading is.
    @ViewBuilder private var guidedCheck: some View {
        Section {
            Label(MeterReadingDraft.banner(kind), systemImage: kind == .voltageDc ? "bolt.fill" : "powerplug.fill")
                .font(.body.weight(.semibold))
                .accessibilityLabel("Power state: " + MeterReadingDraft.banner(kind))
            ForEach(context.safetyNotes.indices, id: \.self) { i in
                Label(context.safetyNotes[i], systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline).foregroundStyle(Palette.caution)
            }
        }
        Section("Do this") {
            if let words = context.instruction?.trimmedOrNil {
                Text(ReportWords.display(words)).font(.subheadline)
            }
            Text(MeterWords.dial(kind))
            ForEach(MeterWords.probes(pins: context.pins, kind: kind), id: \.self) { line in
                Label(line, systemImage: "scope")
            }
            if let good = context.expected?.words {
                Text("A good reading: \(good).").font(.subheadline.weight(.semibold))
            }
        }
    }

    // MARK: Grain's answer

    @ViewBuilder private var verdictSection: some View {
        if savedID != nil {
            let v = verdict ?? ReadingVerdict(state: "pending")
            Section("Grain's answer") {
                HStack(spacing: 12) {
                    switch v.outcome {
                    case .pass: Image(systemName: "checkmark.circle.fill").foregroundStyle(Palette.good)
                    case .fail: Image(systemName: "xmark.octagon.fill").foregroundStyle(Palette.danger)
                    case .unjudged:
                        if v.state == "pending" { ProgressView() } else { Image(systemName: "questionmark.circle").foregroundStyle(Palette.secondary) }
                    }
                    Text(v.headline(expected: context.expected)).font(.body.weight(.semibold))
                }
                .accessibilityElement(children: .combine)
                ForEach(v.notes.indices, id: \.self) { Text(v.notes[$0]).font(.footnote).foregroundStyle(Palette.secondary) }
                if slow, v.state == "pending" {
                    Text("Taking longer than usual. The report updates when Grain answers.").font(.footnote).foregroundStyle(Palette.secondary)
                }
                Text("Your reading is saved. The report updates with it.").font(.footnote).foregroundStyle(Palette.secondary)
            }
        }
    }

    /// Asks the gateway for the verdict every two seconds for two minutes; after that the report is where it appears.
    private func waitForVerdict() async {
        guard let id = savedID, let gateway else { return }
        verdict = ReadingVerdict(state: "pending")
        slow = false
        for round in 0..<60 {
            if let v = try? await gateway.readingVerdict(id: id) {
                verdict = v
                if v.isFinal { return }
            }
            if round == 15 { slow = true }
            try? await Task.sleep(for: .seconds(2))
            if Task.isCancelled { return }
        }
    }

    // MARK: Bluetooth meter

    @ViewBuilder private var meterSection: some View {
        Section(context.isGuided ? "Or use a \(link.profileName)" : link.profileName) {
            switch link.phase {
            case .off, .lost:
                Button("Connect \(link.profileName)") { error = nil; link.start(kind: kind) }
                if let m = link.message { Text(m).font(.footnote).foregroundStyle(.secondary) }
                Text("Snap it onto the back of the phone, switch it on, then connect.")
                    .font(.footnote).foregroundStyle(.secondary)
            case .scanning, .connecting:
                HStack(spacing: 12) {
                    ProgressView()
                    Text(link.phase == .scanning ? "Looking for the meter..." : "Connecting...")
                }
            case .ready, .measuring:
                connected
            }
        }
    }

    @ViewBuilder private var connected: some View {
        let details = [link.model ?? link.deviceName, link.firmware.map { "firmware \($0)" },
                       link.batteryV.map { String(format: "battery %.2f V", $0) }].compactMap { $0 }.joined(separator: ", ")
        if !details.isEmpty { Text(details).font(.footnote).foregroundStyle(.secondary) }
        if let s = link.sample {
            Text(Self.words(s, kind: kind)).font(.title2.monospacedDigit().weight(.semibold))
                .accessibilityLabel("Meter shows " + Self.words(s, kind: kind))
            if let range = s.rangeLabel { Text("Range " + range).font(.footnote).foregroundStyle(.secondary) }
            let usable = MeterReadingDraft.value(from: s, kind: kind)
            if usable == nil {
                Text(s.error ? "The meter flags this value as an error." : "The meter is in another mode; wait a moment.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Button("Use this reading") {
                if let usable { Task { await send(usable, meter: MeterReadingDraft.meter(
                    sample: s, model: link.model, firmware: link.firmware, batteryV: link.batteryV, intervalMs: link.intervalMs),
                    raw: nil) } }
            }
            .disabled(usable == nil)
        } else {
            HStack(spacing: 12) { ProgressView(); Text("Waiting for the first value...") }
        }
    }

    static func words(_ s: MeterSample, kind: MeterReadingKind) -> String {
        if s.error { return "Error" }
        switch kind {
        case .continuity: return s.continuity == true ? "Connected" : "No connection"
        case .voltageDc: return String(format: "%.3f V", s.value)
        case .resistance:
            let v = Double(s.value)
            if v >= 1_000_000 { return String(format: "%.2f M\u{3A9}", v / 1_000_000) }
            if v >= 1000 { return String(format: "%.2f k\u{3A9}", v / 1000) }
            return String(format: "%.1f \u{3A9}", v)
        }
    }

    // MARK: Typed

    @ViewBuilder private var typedSection: some View {
        Section(context.isGuided ? "What does your meter show?" : "Or type what the meter shows") {
            switch kind {
            case .continuity:
                Picker("The meter", selection: $beeps) {
                    Text("Beeps").tag(true)
                    Text("Does not beep").tag(false)
                }
                .pickerStyle(.segmented)
            case .voltageDc:
                TextField("Volts (for example 4.98)", text: $typed).keyboardType(.numbersAndPunctuation)
            case .resistance:
                TextField("Ohms (for example 120)", text: $typed).keyboardType(.numbersAndPunctuation)
            }
            if context.isGuided {
                Text("Any multimeter works: type what it shows. A Bluetooth meter below fills it in for you.")
                    .font(.footnote).foregroundStyle(Palette.secondary)
            }
            Button(context.isGuided ? "Save reading" : "Send typed reading") {
                if let v = MeterReadingDraft.value(typed: typed, kind: kind, beeps: beeps) {
                    Task { await send(v, meter: nil, raw: kind == .continuity ? (beeps ? "beeps" : "no beep") : typed) }
                } else {
                    error = "Enter the number the meter shows."
                }
            }
        }
    }

    // MARK: Send

    private func send(_ value: Value, meter: Meter?, raw: String?) async {
        if let problem = MeterReadingDraft.problem(kind: kind, net: net, pinA: pinA, pinB: pinB) { error = problem; return }
        guard let gateway else { error = "Not connected to the server. Open Settings and connect first."; return }
        error = nil
        sending = true
        defer { sending = false }
        let reading = MeterReadingDraft.reading(context: context, kind: kind, net: net, pinA: pinA, pinB: pinB, scanID: scanID,
                                                sessionID: sessionID, value: value, meter: meter, rawText: raw)
        do {
            try await gateway.postReading(reading)
            sent()
            savedID = reading.readingID
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
