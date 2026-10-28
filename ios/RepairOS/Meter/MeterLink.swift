import CoreBluetooth
import Foundation
import Observation

/// The phone's side of a Bluetooth multimeter: find it, connect, choose what it measures, publish what it reads.
/// Everything runs on the main actor; CoreBluetooth calls back on the main queue (`queue: .main`), which is what
/// makes `MainActor.assumeIsolated` in the delegate methods true by construction, not a way to silence the compiler.
/// The radio is only touched by `start`, so the Bluetooth permission prompt appears when the person asks for a meter.
@MainActor @Observable
final class MeterLink: NSObject {
    enum Phase: Equatable, Sendable {
        /// Not running, or Bluetooth is unavailable (see `message`).
        case off
        case scanning
        case connecting
        /// Connected and configured; waiting for the first value.
        case ready
        case measuring
        /// The connection dropped without `stop()`.
        case lost
    }

    private(set) var phase: Phase = .off
    /// One human sentence about the state, for the sheet. nil when there is nothing to say.
    private(set) var message: String?
    private(set) var sample: MeterSample?
    private(set) var model: String?
    private(set) var firmware: String?
    private(set) var batteryV: Double?
    private(set) var deviceName: String?
    /// What the meter was last told to do, for the `meter` object of a reading.
    private(set) var mode: MeterMode = .idle
    let intervalMs: UInt32 = 500

    private let profile: any MeterProfile
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var characteristics: [CBUUID: CBCharacteristic] = [:]
    private var wantedMode: MeterMode = .idle
    private var stopping = false
    private var scanTimeout: Task<Void, Never>?
    private var configured = false

    init(profile: any MeterProfile = PokitMeterProfile()) {
        self.profile = profile
        super.init()
    }

    var profileName: String { profile.displayName }

    /// Scan for the meter, connect and start it in the mode that answers `kind`. If it is already connected, only the
    /// mode changes.
    func start(kind: MeterReadingKind) {
        wantedMode = MeterMode(kind: kind)
        stopping = false
        if phase == .ready || phase == .measuring {
            sample = nil
            writeSettings()
            return
        }
        guard phase == .off || phase == .lost else { return }
        switch CBManager.authorization {
        case .denied:
            fail("Bluetooth is off for this app. Turn it on in Settings > Privacy & Security > Bluetooth.")
            return
        case .restricted:
            fail("Bluetooth is restricted on this phone.")
            return
        default: break
        }
        message = nil
        if central == nil {
            central = CBCentralManager(delegate: self, queue: .main)   // state arrives in centralManagerDidUpdateState
        } else {
            beginScan()
        }
        phase = .scanning
    }

    /// Put the meter back to idle and disconnect.
    func stop() {
        stopping = true
        scanTimeout?.cancel()
        central?.stopScan()
        if let p = peripheral, p.state == .connected, let c = characteristics[profile.settingsCharacteristic] {
            mode = .idle
            p.writeValue(profile.settingsPayload(mode: .idle, range: profile.autoRange, intervalMs: intervalMs),
                         for: c, type: .withResponse)   // didWriteValueFor then disconnects
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(1))
                self?.disconnectNow()
            }
        } else {
            disconnectNow()
        }
    }

    // MARK: - Steps

    private func fail(_ text: String) {
        message = text
        phase = .off
    }

    private func beginScan() {
        guard let central, central.state == .poweredOn else { return }
        central.scanForPeripherals(withServices: profile.scanServices)
        phase = .scanning
        scanTimeout?.cancel()
        scanTimeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(25))
            guard !Task.isCancelled, let self, self.phase == .scanning else { return }
            self.central?.stopScan()
            self.fail("No \(self.profile.displayName) found. Switch it on and hold it close to the phone.")
        }
    }

    private func disconnectNow() {
        if let p = peripheral, let central, p.state != .disconnected { central.cancelPeripheralConnection(p) }
        finishDisconnect()
    }

    private func finishDisconnect() {
        peripheral = nil
        characteristics = [:]
        configured = false
        sample = nil
        mode = .idle
        phase = .off
        stopping = false
    }

    private func writeSettings() {
        guard let p = peripheral, let c = characteristics[profile.settingsCharacteristic] else { return }
        mode = wantedMode
        p.writeValue(profile.settingsPayload(mode: wantedMode, range: profile.autoRange, intervalMs: intervalMs),
                     for: c, type: .withResponse)
    }

    /// All the characteristics we need are known: read what the meter says about itself, then start it.
    private func configureIfComplete() {
        guard !configured, let p = peripheral,
              characteristics[profile.settingsCharacteristic] != nil,
              let reading = characteristics[profile.readingCharacteristic] else { return }
        configured = true
        for id in [Self.modelNumber, Self.firmwareRevision] {
            if let c = characteristics[id] { p.readValue(for: c) }
        }
        if let b = profile.batteryLocation, let c = characteristics[b.characteristic] { p.readValue(for: c) }
        p.setNotifyValue(true, for: reading)
        writeSettings()
        phase = .ready
    }

    static let modelNumber = CBUUID(string: "2A24")
    static let firmwareRevision = CBUUID(string: "2A26")
    private static let deviceInformation = CBUUID(string: "180A")
}

// MARK: - CoreBluetooth

extension MeterLink: CBCentralManagerDelegate {
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated {
            switch central.state {
            case .poweredOn:
                if phase == .scanning { beginScan() }
            case .poweredOff:
                fail("Bluetooth is switched off. Turn it on in Control Centre.")
            case .unauthorized:
                fail("Bluetooth is off for this app. Turn it on in Settings > Privacy & Security > Bluetooth.")
            case .unsupported:
                fail("This phone cannot use Bluetooth meters.")
            default: break
            }
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        MainActor.assumeIsolated {
            guard phase == .scanning, self.peripheral == nil else { return }
            scanTimeout?.cancel()
            central.stopScan()
            self.peripheral = peripheral
            deviceName = peripheral.name
            peripheral.delegate = self
            phase = .connecting
            central.connect(peripheral)
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        MainActor.assumeIsolated {
            var services = profile.scanServices + [Self.deviceInformation]
            if let b = profile.batteryLocation { services.append(b.service) }
            peripheral.discoverServices(services)
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated {
            self.peripheral = nil
            fail("Could not connect to the \(profile.displayName). Switch it off and on, then try again.")
        }
    }

    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        MainActor.assumeIsolated {
            if stopping {
                finishDisconnect()
            } else {
                peripheral.delegate = nil
                self.peripheral = nil
                characteristics = [:]
                configured = false
                phase = .lost
                message = "The \(profile.displayName) disconnected. Switch it on and tap Connect again."
            }
        }
    }
}

extension MeterLink: CBPeripheralDelegate {
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        MainActor.assumeIsolated {
            for s in peripheral.services ?? [] { peripheral.discoverCharacteristics(nil, for: s) }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        MainActor.assumeIsolated {
            for c in service.characteristics ?? [] { characteristics[c.uuid] = c }
            configureIfComplete()
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            guard error == nil, let data = characteristic.value else { return }
            let id = characteristic.uuid
            if id == profile.readingCharacteristic {
                if let s = profile.parseReading(data, at: Date()) {
                    sample = s
                    if phase == .ready { phase = .measuring }
                }
            } else if id == Self.modelNumber {
                model = String(data: data, encoding: .utf8)
            } else if id == Self.firmwareRevision {
                firmware = String(data: data, encoding: .utf8)
            } else if id == profile.batteryLocation?.characteristic {
                batteryV = profile.parseBattery(data)
            }
        }
    }

    nonisolated func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        MainActor.assumeIsolated {
            if let error {
                message = "The meter did not accept the setting: \(error.localizedDescription)"
            }
            if stopping { disconnectNow() }
        }
    }
}
