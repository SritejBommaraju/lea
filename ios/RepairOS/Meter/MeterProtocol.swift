import CoreBluetooth
import Foundation

/// A Bluetooth multimeter, as bytes. Pure functions only: no radio here, so every line is unit-tested
/// (`RepairOSTests/MeterTests.swift`). `MeterLink` does the talking and works with any profile.
protocol MeterProfile: Sendable {
    /// Shown to the person ("Pokit Meter").
    var displayName: String { get }
    /// What to scan for.
    var scanServices: [CBUUID] { get }
    /// The characteristic the app writes to choose mode, range and update interval.
    var settingsCharacteristic: CBUUID { get }
    /// The characteristic that notifies each reading.
    var readingCharacteristic: CBUUID { get }
    /// Where the battery voltage is read from, when the meter has one.
    var batteryLocation: MeterBatteryLocation? { get }
    func settingsPayload(mode: MeterMode, range: UInt8, intervalMs: UInt32) -> Data
    func parseReading(_ data: Data, at readAt: Date) -> MeterSample?
    /// Battery volts from the status characteristic, or nil when the bytes do not hold a plausible value.
    func parseBattery(_ data: Data) -> Double?
}

/// UUIDs are kept as strings because `CBUUID` is not `Sendable`; the typed values are made on demand.
struct MeterBatteryLocation: Sendable {
    let serviceUUID: String
    let characteristicUUID: String
    var service: CBUUID { CBUUID(string: serviceUUID) }
    var characteristic: CBUUID { CBUUID(string: characteristicUUID) }
}

extension MeterProfile {
    /// Range byte for "let the meter choose".
    var autoRange: UInt8 { 255 }
    func parseReading(_ data: Data) -> MeterSample? { parseReading(data, at: Date()) }
}

/// What a meter can be asked to measure. The raw values are the Pokit Meter's mode numbers.
enum MeterMode: UInt8, Sendable, CaseIterable {
    case idle = 0, dcVoltage, acVoltage, dcCurrent, acCurrent, resistance, diode, continuity, temperature

    /// The contract's name for it (`meter.mode`); nil for idle.
    var contract: Mode? {
        switch self {
        case .idle: nil
        case .dcVoltage: .dcVoltage
        case .acVoltage: .acVoltage
        case .dcCurrent: .dcCurrent
        case .acCurrent: .acCurrent
        case .resistance: .resistance
        case .diode: .diode
        case .continuity: .continuity
        case .temperature: .temperature
        }
    }

    /// The mode that answers a reading of this kind.
    init(kind: MeterReadingKind) {
        switch kind {
        case .continuity: self = .continuity
        case .voltageDc: self = .dcVoltage
        case .resistance: self = .resistance
        }
    }
}

/// One value from the meter.
struct MeterSample: Equatable, Sendable {
    var mode: MeterMode
    /// Volts, amps, ohms or degrees, as the mode says. For continuity, the meter's own number.
    var value: Float
    /// Continuity mode only: the meter says there is (true) or is not (false) a connection.
    var continuity: Bool?
    var autoRange: Bool
    /// The meter flagged this value as an error (over range, probes open in a mode that needs a signal).
    var error: Bool
    /// The range the meter used, as its own label ("2-6 V"), when the profile knows it.
    var rangeLabel: String?
    var readAt: Date
}

/// Pokit Meter, from its published Bluetooth API ("Pokit Meter Bluetooth API Documentation", D0005250 rev 0.02).
/// All numbers little-endian.
struct PokitMeterProfile: MeterProfile {
    let displayName = "Pokit Meter"
    var scanServices: [CBUUID] { [CBUUID(string: "E7481D2F-5781-442E-BB9A-FD4E3441DADC")] }
    var settingsCharacteristic: CBUUID { CBUUID(string: "53DC9A7A-BC19-4280-B76B-002D0E23B078") }
    var readingCharacteristic: CBUUID { CBUUID(string: "047D3559-8BEE-423A-B229-4417FA603B90") }
    var batteryLocation: MeterBatteryLocation? {
        MeterBatteryLocation(serviceUUID: "57D3A771-267C-4394-8872-78223E92AEC4",
                             characteristicUUID: "3DBA36E1-6120-4706-8DFD-ED9C16E569B6")
    }

    /// Settings, 6 bytes: mode (u8), range (u8), update interval in ms (u32).
    func settingsPayload(mode: MeterMode, range: UInt8, intervalMs: UInt32) -> Data {
        var bytes: [UInt8] = [mode.rawValue, range]
        for shift in stride(from: 0, to: 32, by: 8) { bytes.append(UInt8((intervalMs >> UInt32(shift)) & 0xFF)) }
        return Data(bytes)
    }

    /// Reading, 7 bytes: status (u8), value (f32), mode (u8), range (u8). Status: 255 is an error; for
    /// continuity 1 is "connected" and 0 "not"; for the rest 1 means auto-range is on.
    func parseReading(_ data: Data, at readAt: Date) -> MeterSample? {
        let b = [UInt8](data)
        guard b.count >= 7, let mode = MeterMode(rawValue: b[5]) else { return nil }
        let value = Self.float(b[1...4])
        let status = b[0]
        let isContinuity = mode == .continuity
        return MeterSample(mode: mode, value: value,
                           continuity: isContinuity && status != 255 ? status == 1 : nil,
                           autoRange: !isContinuity && status == 1,
                           error: status == 255,
                           rangeLabel: Self.rangeLabel(mode: mode, range: b[6]), readAt: readAt)
    }

    /// The status characteristic ends with the battery voltage as a float, 0.0 to 3.3 V.
    func parseBattery(_ data: Data) -> Double? {
        let b = [UInt8](data)
        guard b.count >= 4 else { return nil }
        let v = Self.float(b.suffix(4))
        return v.isFinite && v >= 0 && v <= 4.0 ? Double(v) : nil
    }

    private static func float(_ four: ArraySlice<UInt8>) -> Float {
        var bits: UInt32 = 0
        for (i, byte) in four.enumerated() { bits |= UInt32(byte) << UInt32(8 * i) }
        return Float(bitPattern: bits)
    }

    private static let voltageRanges = ["0-300 mV", "300 mV-2 V", "2-6 V", "6-12 V", "12-30 V", "30-60 V"]
    private static let resistanceRanges = ["0-160 \u{3A9}", "160-330 \u{3A9}", "330-890 \u{3A9}", "890 \u{3A9}-1.5 k\u{3A9}",
                                           "1.5-10 k\u{3A9}", "10-100 k\u{3A9}", "100-470 k\u{3A9}", "470 k\u{3A9}-1 M\u{3A9}"]

    static func rangeLabel(mode: MeterMode, range: UInt8) -> String? {
        let table: [String]
        switch mode {
        case .dcVoltage, .acVoltage: table = voltageRanges
        case .resistance: table = resistanceRanges
        default: return nil
        }
        return Int(range) < table.count ? table[Int(range)] : nil
    }
}
