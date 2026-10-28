import CoreBluetooth
import Foundation

/// Where to find the product over Bluetooth and which characteristics to use, from the report's `self_test.ble`. The
/// app holds no UUID of its own: a product that does not say is not asked.
struct SelfTestLinkConfig: Equatable, Sendable {
    var service: String
    var result: String
    var control: String
    var namePrefix: String?

    /// nil when the plan has no Bluetooth block or one of its UUIDs is not a full 128-bit UUID (a malformed string
    /// would crash `CBUUID`, so it is checked first).
    init?(plan: SelfTest) {
        guard let ble = plan.ble, plan.transports.contains(.ble) else { return nil }
        let all = [ble.service, ble.result, ble.control]
        guard all.allSatisfy({ UUID(uuidString: $0) != nil }) else { return nil }
        service = ble.service
        result = ble.result
        control = ble.control
        namePrefix = ble.namePrefix?.trimmedOrNil
    }

    /// Whether an advertised name is the product's: any name when the plan gives no prefix.
    func accepts(name: String?) -> Bool {
        guard let namePrefix else { return true }
        return name?.hasPrefix(namePrefix) ?? false
    }

    var serviceUUID: CBUUID { CBUUID(string: service) }
    var resultUUID: CBUUID { CBUUID(string: result) }
    var controlUUID: CBUUID { CBUUID(string: control) }
}

/// The phone's side of the product's self-test: find the product, subscribe to its result, write the start byte, read
/// the answer. Everything runs on the main actor; CoreBluetooth calls back on the main queue (`queue: .main`), so the
/// delegate conformances are `@preconcurrency`: the compiler checks at run time that each callback really is on the
/// main actor. The radio is only touched by `start`, so the Bluetooth prompt appears when the person asks for the
/// self-test. Not run in the simulator, which has no Bluetooth: the bytes are decoded by `SelfTestFrame`, which is
/// unit-tested.
@MainActor
final class MeterLink: NSObject, ObservableObject {
    enum Phase: Equatable, Sendable {
        /// Not running, or Bluetooth is unavailable (see `message`).
        case off
        case scanning
        case connecting
        /// Connected, the start byte written; waiting for the product's answer.
        case running
        /// The product answered; `frame` holds it.
        case done
        /// The connection dropped before the answer.
        case lost
    }

    @Published private(set) var phase: Phase = .off
    /// One human sentence about the state, for the sheet. nil when there is nothing to say.
    @Published private(set) var message: String?
    @Published private(set) var frame: SelfTestFrame?
    @Published private(set) var deviceName: String?

    /// The start byte the product's control characteristic takes.
    static let startByte = Data([0x01])

    private var config: SelfTestLinkConfig?
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var characteristics: [CBUUID: CBCharacteristic] = [:]
    private var candidates: [UUID: (peripheral: CBPeripheral, rssi: Int)] = [:]
    private var chooser: Task<Void, Never>?
    private var scanTimeout: Task<Void, Never>?
    private var answerPoll: Task<Void, Never>?
    /// The start byte was written: only a result read after that is this run's.
    private var armed = false
    private var stopping = false
    private var started = false

    /// Scan for the product, connect, and ask it to check itself.
    func start(_ config: SelfTestLinkConfig) {
        guard phase == .off || phase == .lost || phase == .done else { return }
        cancelTimers()   // a run that is starting again begins with no timer left from the last one
        self.config = config
        frame = nil
        message = nil
        deviceName = nil
        armed = false
        stopping = false
        started = false
        candidates = [:]
        switch CBManager.authorization {
        case .denied:
            fail("Bluetooth is off for this app. Turn it on in Settings > Privacy & Security > Bluetooth.")
            return
        case .restricted:
            fail("Bluetooth is restricted on this phone.")
            return
        default: break
        }
        phase = .scanning
        if central == nil {
            central = CBCentralManager(delegate: self, queue: .main)   // state arrives in centralManagerDidUpdateState
        } else {
            beginScan()
        }
    }

    /// Stop and disconnect.
    func stop() {
        stopping = true
        cancelTimers()
        central?.stopScan()
        disconnectNow()
    }

    // MARK: - Steps

    private func fail(_ text: String) {
        cancelTimers()
        message = text
        phase = .off
    }

    private func cancelTimers() {
        chooser?.cancel()
        scanTimeout?.cancel()
        answerPoll?.cancel()
        chooser = nil
        scanTimeout = nil
        answerPoll = nil
    }

    private func beginScan() {
        guard let central, central.state == .poweredOn, let config else { return }
        central.scanForPeripherals(withServices: [config.serviceUUID])
        phase = .scanning
        scanTimeout?.cancel()
        scanTimeout = Task { [weak self] in
            try? await Task.sleep(for: .seconds(25))
            guard !Task.isCancelled, let self, self.phase == .scanning else { return }
            self.central?.stopScan()
            let who = self.config?.namePrefix.map { "No product named \($0)\u{2026} found." } ?? "No product found."
            self.fail(who + " Switch it on and hold it close to the phone.")
        }
    }

    /// Several products may be in range (a shelf of them): the closest, by signal strength, is the one in the person's hand.
    private func connectClosest() {
        chooser = nil
        guard phase == .scanning, peripheral == nil, let best = candidates.values.max(by: { $0.rssi < $1.rssi }) else { return }
        scanTimeout?.cancel()
        central?.stopScan()
        peripheral = best.peripheral
        deviceName = best.peripheral.name
        best.peripheral.delegate = self
        phase = .connecting
        central?.connect(best.peripheral)
    }

    private func disconnectNow() {
        if let p = peripheral, let central, p.state != .disconnected { central.cancelPeripheralConnection(p) }
        finishDisconnect()
    }

    private func finishDisconnect() {
        peripheral = nil
        characteristics = [:]
        started = false
        armed = false
        if phase != .done { phase = .off }
        stopping = false
    }

    /// Both characteristics are known: subscribe to the result first, then write the start byte.
    private func subscribeIfComplete() {
        guard !started, let p = peripheral, let config,
              let result = characteristics[config.resultUUID], characteristics[config.controlUUID] != nil else { return }
        started = true
        if result.properties.contains(.notify) || result.properties.contains(.indicate) {
            p.setNotifyValue(true, for: result)   // the start byte is written once the subscription is confirmed
        } else {
            writeStart()   // a result that can only be read: the polling after the start byte reads it
        }
    }

    private func writeStart() {
        guard let p = peripheral, let config, let control = characteristics[config.controlUUID] else { return }
        let canRespond = control.properties.contains(.write)
        p.writeValue(Self.startByte, for: control, type: canRespond ? .withResponse : .withoutResponse)
        if !canRespond { armedNow() }
    }

    /// The start byte is on its way: the answer is read when the product notifies, and by reading every second as well,
    /// for a product that only lets the result be read.
    private func armedNow() {
        armed = true
        phase = .running
        answerPoll?.cancel()
        answerPoll = Task { [weak self] in
            for _ in 0..<12 {
                try? await Task.sleep(for: .seconds(1))
                guard !Task.isCancelled, let self, self.phase == .running else { return }
                if let p = self.peripheral, let id = self.config?.resultUUID, let c = self.characteristics[id] { p.readValue(for: c) }
            }
            guard !Task.isCancelled, let self, self.phase == .running else { return }
            self.message = "The product did not answer. Switch it off and on, then try again."
            self.disconnectNow()
        }
    }

    private func received(_ data: Data) {
        guard armed, phase == .running else { return }
        switch SelfTestFrame.decode(data) {
        case .success(let f):
            answerPoll?.cancel()
            frame = f
            message = nil
            phase = .done
            disconnectNow()
        case .failure(.truncated), .failure(.tooShort):
            break   // not all of it yet: the next notification or read has the rest
        case .failure(let why):
            message = "The product sent an answer this app does not understand (\(why)). Update the app and try again."
            answerPoll?.cancel()
            disconnectNow()
        }
    }
}

// MARK: - CoreBluetooth

extension MeterLink: @preconcurrency CBCentralManagerDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            if phase == .scanning { beginScan() }
        case .poweredOff:
            fail("Bluetooth is switched off. Turn it on in Control Centre.")
        case .unauthorized:
            fail("Bluetooth is off for this app. Turn it on in Settings > Privacy & Security > Bluetooth.")
        case .unsupported:
            fail("This phone cannot use Bluetooth.")
        default: break
        }
    }

    func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard phase == .scanning, self.peripheral == nil, let config else { return }
        let name = peripheral.name ?? advertisementData[CBAdvertisementDataLocalNameKey] as? String
        guard config.accepts(name: name) else { return }
        candidates[peripheral.identifier] = (peripheral, RSSI.intValue)
        if chooser == nil {
            chooser = Task { [weak self] in
                try? await Task.sleep(for: .milliseconds(1500))
                guard !Task.isCancelled else { return }
                self?.connectClosest()
            }
        }
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        guard let config else { return }
        peripheral.discoverServices([config.serviceUUID])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        self.peripheral = nil
        fail("Could not connect to the product. Switch it off and on, then try again.")
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        if stopping || phase == .done || phase == .off {
            finishDisconnect()
        } else {
            cancelTimers()
            peripheral.delegate = nil
            self.peripheral = nil
            characteristics = [:]
            started = false
            armed = false
            phase = .lost
            message = "The product disconnected before it answered. Tap the self-test again."
        }
    }
}

extension MeterLink: @preconcurrency CBPeripheralDelegate {
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let config else { return }
        for s in peripheral.services ?? [] {
            peripheral.discoverCharacteristics([config.resultUUID, config.controlUUID], for: s)
        }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        for c in service.characteristics ?? [] { characteristics[c.uuid] = c }
        subscribeIfComplete()
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            fail("The product would not send its answer: \(error.localizedDescription)")
            disconnectNow()
            return
        }
        if characteristic.uuid == config?.resultUUID, characteristic.isNotifying, !armed { writeStart() }
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        if let error {
            fail("The product did not accept the start: \(error.localizedDescription)")
            disconnectNow()
            return
        }
        if characteristic.uuid == config?.controlUUID, !armed { armedNow() }
    }

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard error == nil, characteristic.uuid == config?.resultUUID, let data = characteristic.value else { return }
        received(data)
    }
}
