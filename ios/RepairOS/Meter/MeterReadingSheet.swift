import SwiftUI

/// What a check is about, so the sheet can open already filled in. Built from a `NextAction` (kind, net, pins, the
/// action it answers) or from the report's next step (kind, action, the net and probe points when the design engine
/// named them, which phone check settles it) plus the product's self-test plan from the report.
struct MeterReadingContext: Identifiable {
    let id = UUID()
    var actionID: String?
    var kind: MultimeterKind
    var net: String
    var pins: [String]
    /// What to watch out for before probing, shown above the probe points (the report's `next_step.safety_notes`).
    var safetyNotes: [String]
    /// The design engine's own words for the check (`next_step.instruction`).
    var instruction: String?
    /// What a good reading looks like (`next_step.expected`), said before measuring and beside the verdict.
    var expected: ExpectedReading?
    /// How the product checks itself (report `self_test`); nil when the design has no self-test.
    var selfTest: SelfTest?
    /// The check that settles the step (`next_step.target.instrument`); its section opens first.
    var instrument: Instrument?
    /// The parts the step is about (`next_step.target.part_refs`): the first one fills in the part field.
    var parts: [String]

    init(kind: MultimeterKind = .continuity, net: String = "", pins: [String] = [], actionID: String? = nil,
         safetyNotes: [String] = [], instruction: String? = nil, expected: ExpectedReading? = nil,
         selfTest: SelfTest? = nil, instrument: Instrument? = nil, parts: [String] = []) {
        self.kind = kind
        self.net = net
        self.pins = pins
        self.actionID = actionID
        self.safetyNotes = safetyNotes
        self.instruction = instruction
        self.expected = expected
        self.selfTest = selfTest
        self.instrument = instrument
        self.parts = parts
    }

    /// A check the report named: the net, the two probe points and the check's id are known, so the typed section walks
    /// the customer through it.
    var isGuided: Bool { actionID != nil && !net.isEmpty && pins.count == 2 }

    init(action: NextAction) {
        self.init(kind: Self.kind(forAction: action.kind.rawValue) ?? .continuity, net: action.target?.net ?? "",
                  pins: action.target?.pins ?? [], actionID: action.actionID, parts: action.target?.partRefs ?? [])
    }

    init(step: ReportModel.NextStep, selfTest: SelfTest? = nil) {
        self.init(kind: Self.kind(forAction: step.kind) ?? .continuity, net: step.net ?? "", pins: step.pins,
                  actionID: step.actionID, safetyNotes: step.safetyNotes, instruction: step.instruction,
                  expected: step.expected, selfTest: selfTest, instrument: step.instrument.flatMap(Instrument.init(rawValue:)),
                  parts: step.partRefs)
    }

    static func kind(forAction kind: String) -> MultimeterKind? {
        switch kind {
        case "run_continuity_test": .continuity
        case "run_resistance_test": .resistance
        case "run_voltage_test": .voltageDc
        default: nil
        }
    }

    /// The section that opens first: the one the step names if the product can do it, else the product's own
    /// self-test (Bluetooth before the light), else none.
    var firstInstrument: Instrument? {
        if let instrument, offers(instrument) { return instrument }
        if offers(.selftestBle) { return .selftestBle }
        if offers(.selftestLed) { return .selftestLed }
        return nil
    }

    /// Whether the sheet has a section for it (the phone's sensors and the typed value always; the self-test only when
    /// the plan has the block it needs).
    func offers(_ i: Instrument) -> Bool {
        switch i {
        case .selftestBle: selfTest.map { SelfTestLinkConfig(plan: $0) != nil } ?? false
        case .selftestLED: selfTest.map { $0.transports.contains(.led) && $0.blink.flatMap(BlinkCode.init(plan:)) != nil } ?? false
        default: true
        }
    }
}

extension ReportModel {
    /// The sheet's context for this report: the step it names, if any, and the product's self-test.
    var meterContext: MeterReadingContext {
        nextStep.map { MeterReadingContext(step: $0, selfTest: selfTest) } ?? MeterReadingContext(selfTest: selfTest)
    }
}

/// The rules of a typed meter reading, apart from the screen so they can be tested.
enum MeterReadingDraft {
    /// Continuity and resistance are read with the product off, voltage with it on (invariant S4).
    static func powerState(_ kind: MultimeterKind) -> PowerStateEnum {
        kind == .voltageDc ? .on : .off
    }

    /// Shown before the probe points.
    static func banner(_ kind: MultimeterKind) -> String {
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
    static func problem(kind: MultimeterKind, net: String, pinA: String, pinB: String) -> String? {
        if net.trimmingCharacters(in: .whitespaces).isEmpty { return "Enter the net name." }
        let p = pins(pinA, pinB)
        if let p, p.contains(where: \.isEmpty) { return "Enter both probe points, or neither." }
        if p == nil, kind != .voltageDc { return "Enter the two probe points." }
        return nil
    }

    /// A typed value ("4.98", "4,98", "120") for this kind; nil when it is not a number the kind allows.
    static func value(typed text: String, kind: MultimeterKind, beeps: Bool) -> Value? {
        if kind == .continuity { return Value(volts: nil, continuity: beeps, ohms: nil) }
        let t = text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
        guard let x = Double(t), x.isFinite else { return nil }
        if kind == .voltageDc { return Value(volts: x, continuity: nil, ohms: nil) }
        return x >= 0 ? Value(volts: nil, continuity: nil, ohms: x) : nil
    }

    /// The reading for a value the person typed from their own meter.
    static func reading(context: MeterReadingContext, kind: MultimeterKind, net: String, pinA: String, pinB: String,
                        scanID: String, sessionID: String, value: Value, rawText: String?,
                        now: Date = Date(), id: String = "rd_" + ScanStorage.hex(8)) -> MeterReading {
        MeterReading(actionID: context.actionID, condition: nil, expected: nil, kind: kind.contract, meter: nil,
                     net: net.trimmingCharacters(in: .whitespaces), part: nil, pins: pins(pinA, pinB), powerState: powerState(kind),
                     rawText: rawText, readingID: id, recordedAt: now, scanID: scanID, sessionID: sessionID,
                     source: .typed, value: value, verdict: nil)
    }
}

extension Value {
    /// A multimeter value (the generated memberwise init also lists the self-test and phone-sensor fields).
    init(volts: Double?, continuity: Bool?, ohms: Double?) {
        self.init(volts: volts, continuity: continuity, ohms: ohms, index: nil, ok: nil, state: nil, transport: nil,
                  valueMv: nil, deltaUT: nil, hz: nil, depth: nil, color: nil)
    }
}

/// "Check it with your phone": the product's own self-test (over Bluetooth, or as blinks of its status light the camera
/// reads) and the phone's sensors, with typing what a meter shows as the last, optional way. Nothing here needs to be
/// bought. The readings go to the gateway, which judges them against the design (the phone never decides).
struct MeterReadingSheet: View {
    let context: MeterReadingContext
    let scanID: String
    let sessionID: String
    let gateway: GatewayClient?
    /// Called once the gateway has accepted a reading; the report then says it was sent.
    let sent: () -> Void

    @Environment(\.dismiss) private var dismiss
    /// The one section that is open (only one camera or microphone runs at a time).
    @State private var open: Instrument?
    @State private var part: String
    @State private var kind: MultimeterKind
    @State private var net: String
    @State private var pinA: String
    @State private var pinB: String
    @State private var typed = ""
    @State private var beeps = true
    @State private var typedSaved = false
    @State private var sending = false
    @State private var error: String?
    /// The last reading the gateway has accepted, and what the design engine said about it.
    @State private var savedID: String?
    @State private var verdict: ReadingVerdict?
    @State private var slow = false
    @State private var readingsSent = 0

    init(context: MeterReadingContext, scanID: String, sessionID: String, gateway: GatewayClient?, sent: @escaping () -> Void) {
        self.context = context
        self.scanID = scanID
        self.sessionID = sessionID
        self.gateway = gateway
        self.sent = sent
        _open = SwiftUI.State(initialValue: context.firstInstrument)
        _part = SwiftUI.State(initialValue: context.parts.first ?? "")
        _kind = SwiftUI.State(initialValue: context.kind)
        _net = SwiftUI.State(initialValue: context.net)
        _pinA = SwiftUI.State(initialValue: context.pins.first ?? "")
        _pinB = SwiftUI.State(initialValue: context.pins.dropFirst().first ?? "")
    }

    private var env: PhoneCheckEnv {
        PhoneCheckEnv(actionID: context.actionID, scanID: scanID, sessionID: sessionID) { readings in await post(readings) }
    }

    var body: some View {
        NavigationStack {
            Form {
                introSection
                if let plan = context.selfTest {
                    Section("The product checks itself") {
                        if context.offers(.selftestBle) {
                            group(.selftestBle, "Over Bluetooth", "dot.radiowaves.left.and.right") {
                                SelfTestBluetoothSection(plan: plan, env: env)
                            }
                        }
                        if context.offers(.selftestLED), let code = plan.blink.flatMap(BlinkCode.init(plan:)) {
                            group(.selftestLED, "Through its status light", "lightbulb.led.fill") {
                                SelfTestBlinkSection(plan: plan, code: code, env: env)
                            }
                        }
                    }
                }
                Section("The phone measures") {
                    PartField(part: $part)
                    group(.magnetometer, "Current in a motor or wire", "bolt.horizontal.circle") {
                        MagnetometerSection(env: env, part: part)
                    }
                    group(.audio, "Sound of a motor or fan", "waveform") { AudioSection(env: env, part: part) }
                    group(.flicker, "Flicker of a light", "sun.max") { FlickerSection(env: env, part: part) }
                    group(.light, "Is the light on", "lightbulb") { LightSection(env: env, part: part) }
                    group(.led, "What an LED is doing", "light.max") { LedStateSection(env: env, part: part) }
                }
                Section("Have a meter already?") {
                    group(.typed, "Type what your meter shows", "keyboard") { typedContent }
                }
                verdictSection
                if let error {
                    Section { Text(error).foregroundStyle(Palette.danger) }
                }
            }
            .disabled(sending)
            .navigationTitle("Check it with your phone")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(readingsSent == 0 ? "Cancel" : "Done") { dismiss() }.fontWeight(readingsSent == 0 ? .regular : .semibold)
                }
            }
            .task(id: savedID) { await waitForVerdict() }
        }
    }

    // MARK: Sections

    @ViewBuilder private var introSection: some View {
        Section {
            Text("Nothing to buy: the product and your phone do the checking. Your phone asks for the camera or microphone only when you tap a check that needs it.")
                .font(.subheadline)
            if let words = context.instruction?.trimmedOrNil {
                Text(ReportWords.display(words)).font(.subheadline)
            }
            if let good = context.expected?.words {
                Text("A good reading: \(good).").font(.subheadline.weight(.semibold))
            }
            ForEach(context.safetyNotes.indices, id: \.self) { i in
                Label(context.safetyNotes[i], systemImage: "exclamationmark.triangle.fill")
                    .font(.subheadline).foregroundStyle(Palette.caution)
            }
            if context.selfTest == nil {
                Text("This product has no self-test, so the checks below use the phone's own sensors.")
                    .font(.footnote).foregroundStyle(Palette.secondary)
            }
        }
    }

    private func group<Content: View>(_ i: Instrument, _ title: String, _ symbol: String,
                                      @ViewBuilder _ content: @escaping () -> Content) -> some View {
        DisclosureGroup(isExpanded: Binding(get: { open == i }, set: { on in
            if on { open = i } else if open == i { open = nil }
        })) {
            content()
        } label: {
            Label(title, systemImage: symbol)
        }
    }

    // MARK: Typed (any multimeter, optional)

    @ViewBuilder private var typedContent: some View {
        if context.isGuided {
            Label(MeterReadingDraft.banner(kind), systemImage: kind == .voltageDc ? "bolt.fill" : "powerplug.fill")
                .font(.body.weight(.semibold))
                .accessibilityLabel("Power state: " + MeterReadingDraft.banner(kind))
            Text(MeterWords.dial(kind))
            ForEach(MeterWords.probes(pins: context.pins, kind: kind), id: \.self) { line in
                Label(line, systemImage: "scope")
            }
        } else {
            Picker("Measuring", selection: $kind) {
                Text("Continuity").tag(MultimeterKind.continuity)
                Text("DC voltage").tag(MultimeterKind.voltageDc)
                Text("Resistance").tag(MultimeterKind.resistance)
            }
            .pickerStyle(.segmented)
            Label(MeterReadingDraft.banner(kind), systemImage: kind == .voltageDc ? "bolt.fill" : "powerplug.fill")
                .font(.body.weight(.semibold))
            TextField("Net name", text: $net).textInputAutocapitalization(.characters).autocorrectionDisabled()
            TextField(kind == .voltageDc ? "Probe + (for example J1.1)" : "First point (for example J1.1)", text: $pinA)
                .textInputAutocapitalization(.characters).autocorrectionDisabled()
            TextField(kind == .voltageDc ? "Probe - (for example J1.2)" : "Second point", text: $pinB)
                .textInputAutocapitalization(.characters).autocorrectionDisabled()
        }
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
        Button(typedSaved ? "Sent" : "Send typed reading") {
            if let v = MeterReadingDraft.value(typed: typed, kind: kind, beeps: beeps) {
                Task { await sendTyped(v, raw: kind == .continuity ? (beeps ? "beeps" : "no beep") : typed) }
            } else {
                error = "Enter the number the meter shows."
            }
        }
        .disabled(typedSaved)
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
                Text("Your \(readingsSent == 1 ? "reading is" : "readings are") saved. The report updates with \(readingsSent == 1 ? "it" : "them").")
                    .font(.footnote).foregroundStyle(Palette.secondary)
            }
        }
    }

    /// Asks the gateway for the verdict while the sheet is open: every two seconds for the first minute, then every
    /// five (the design engine may take minutes). Closing the sheet ends it; the report shows the answer either way.
    private func waitForVerdict() async {
        guard let id = savedID, let gateway else { return }
        verdict = ReadingVerdict(state: "pending")
        slow = false
        var round = 0
        while !Task.isCancelled {
            if let v = try? await gateway.readingVerdict(id: id) {
                verdict = v
                if v.isFinal { return }
            }
            round += 1
            if round >= 15 { slow = true }
            try? await Task.sleep(for: .seconds(round < 30 ? 2 : 5))
        }
    }

    // MARK: Send

    /// Sends readings one after the other; true when the gateway accepted all of them.
    private func post(_ readings: [MeterReading]) async -> Bool {
        guard !readings.isEmpty else { return false }
        guard let gateway else {
            error = "Not connected to the server. Open Settings and connect first."
            return false
        }
        error = nil
        sending = true
        defer { sending = false }
        for r in readings {
            do {
                try await gateway.postReading(r)
                sent()
                readingsSent += 1
                savedID = r.readingID
            } catch {
                self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
                return false
            }
        }
        return true
    }

    private func sendTyped(_ value: Value, raw: String?) async {
        if let problem = MeterReadingDraft.problem(kind: kind, net: net, pinA: pinA, pinB: pinB) { error = problem; return }
        let reading = MeterReadingDraft.reading(context: context, kind: kind, net: net, pinA: pinA, pinB: pinB, scanID: scanID,
                                                sessionID: sessionID, value: value, rawText: raw)
        typedSaved = await post([reading])
    }
}
