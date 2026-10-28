import Foundation

/// The phone's magnetometer as a current detector: a motor, a coil or a wire carrying current moves the field the
/// phone feels. Pure rules, unit-tested (`RepairOSTests/PhoneSensorTests.swift`); `MagnetometerCheck` feeds it.
struct MagneticBaseline: Equatable, Sendable {
    /// Average field strength (magnitude of the three axes) in microtesla.
    var mean: Double
    /// Standard deviation of the field strength over the baseline, in microtesla: the phone's own noise there.
    var noise: Double
    var count: Int

    /// The change that counts as current: 5 microtesla, or 3 times the noise measured in the baseline, whichever is
    /// larger. Written in PLAN.md as well.
    var threshold: Double { max(Magnetometer.minimumDeltaUT, Magnetometer.noiseFactor * noise) }

    /// nil with fewer than 20 samples (a baseline of a second at the lowest rate).
    init?(magnitudes: [Double]) {
        guard magnitudes.count >= 20 else { return nil }
        let n = Double(magnitudes.count)
        let mean = magnitudes.reduce(0, +) / n
        let variance = magnitudes.reduce(0) { $0 + ($1 - mean) * ($1 - mean) } / n
        self.mean = mean
        self.noise = variance.squareRoot()
        self.count = magnitudes.count
    }
}

enum Magnetometer {
    static let minimumDeltaUT = 5.0
    static let noiseFactor = 3.0

    /// Field strength of one reading, in microtesla.
    static func magnitude(x: Double, y: Double, z: Double) -> Double {
        (x * x + y * y + z * z).squareRoot()
    }

    /// How far the live field is from the baseline: the average of the recent samples against the baseline's average,
    /// always positive. nil without samples.
    static func delta(baseline: MagneticBaseline, recent: [Double]) -> Double? {
        guard !recent.isEmpty else { return nil }
        return abs(recent.reduce(0, +) / Double(recent.count) - baseline.mean)
    }

    static func state(delta: Double, baseline: MagneticBaseline) -> ValueState {
        delta >= baseline.threshold ? .current : .none
    }
}

/// Relative brightness from the camera's own exposure. iOS has no public lux sensor, so this is a ratio against a
/// baseline taken with the product off, never a lux number. The camera chooses ISO and exposure time to make the
/// picture a fixed brightness, so scene light is about (picture brightness) / (ISO x exposure time), with the
/// picture's gamma taken out.
enum RelativeLight {
    /// Below this ratio over the baseline the light did not change (the camera's own exposure wobbles about 10 to 20 %).
    static let unchangedBelow = 1.3
    /// From `unchangedBelow` up to this ratio it is dim; at or above, on.
    static let dimBelow = 4.0

    /// nil when the camera reports no usable exposure.
    static func level(meanBrightness: Float, iso: Float, exposureS: Double) -> Double? {
        guard iso > 0, exposureS > 0, meanBrightness > 0 else { return nil }
        return pow(Double(meanBrightness), 2.2) / (Double(iso) * exposureS)
    }

    static func state(ratio: Double) -> ValueState {
        if ratio < unchangedBelow { return .off }
        return ratio < dimBelow ? .dim : .on
    }
}

extension Value {
    static func magnetometer(deltaUT: Double, state: ValueState) -> Value {
        Value(volts: nil, continuity: nil, ohms: nil, index: nil, ok: nil, state: state, transport: nil, valueMv: nil,
              deltaUT: deltaUT, hz: nil, depth: nil, color: nil)
    }

    static func audio(hz: Double) -> Value {
        Value(volts: nil, continuity: nil, ohms: nil, index: nil, ok: nil, state: nil, transport: nil, valueMv: nil,
              deltaUT: nil, hz: hz, depth: nil, color: nil)
    }

    static func flicker(hz: Double, depth: Double) -> Value {
        Value(volts: nil, continuity: nil, ohms: nil, index: nil, ok: nil, state: nil, transport: nil, valueMv: nil,
              deltaUT: nil, hz: hz, depth: depth, color: nil)
    }

    static func light(state: ValueState) -> Value {
        Value(volts: nil, continuity: nil, ohms: nil, index: nil, ok: nil, state: state, transport: nil, valueMv: nil,
              deltaUT: nil, hz: nil, depth: nil, color: nil)
    }

    static func led(state: ValueState, color: String?) -> Value {
        Value(volts: nil, continuity: nil, ohms: nil, index: nil, ok: nil, state: state, transport: nil, valueMv: nil,
              deltaUT: nil, hz: nil, depth: nil, color: color)
    }
}

/// A reading the phone's own sensor took. The product is on while the person measures, and nothing here is typed.
enum PhoneReadings {
    static func reading(kind: MeterReadingKind, part: String, value: Value, actionID: String?, scanID: String, sessionID: String,
                        now: Date = Date(), id: String = "rd_" + ScanStorage.hex(8)) -> MeterReading {
        MeterReading(actionID: actionID, condition: nil, expected: nil, kind: kind, meter: nil, net: nil,
                     part: part.trimmingCharacters(in: .whitespaces), pins: nil, powerState: .on, rawText: nil, readingID: id,
                     recordedAt: now, scanID: scanID, sessionID: sessionID, source: .phone, value: value, verdict: nil)
    }

    /// One decimal place for hertz and microtesla, two for a ratio: the sensors are not better than that.
    static func rounded(_ x: Double, places: Int) -> Double {
        let f = pow(10.0, Double(places))
        return (x * f).rounded() / f
    }
}
