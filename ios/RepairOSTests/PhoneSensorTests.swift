import Foundation
import Testing
@testable import RepairOS

/// The rules behind the phone's own sensors: the magnetometer's current detector, the camera's relative light, and the
/// readings they send. The sensors themselves (magnetometer, camera, microphone) do not exist in the simulator.
struct PhoneSensorTests {
    // MARK: Magnetometer

    @Test func theThresholdIsFiveMicroteslaOrThreeTimesTheNoiseWhicheverIsLarger() throws {
        let quiet = try #require(MagneticBaseline(magnitudes: [Double](repeating: 47, count: 100)))
        #expect(quiet.noise == 0 && quiet.threshold == 5 && abs(quiet.mean - 47) < 1e-9)
        // 44 and 50 alternate: mean 47, standard deviation 3, so three times the noise is 9
        let noisy = try #require(MagneticBaseline(magnitudes: (0..<100).map { $0 % 2 == 0 ? 44.0 : 50.0 }))
        #expect(abs(noisy.noise - 3) < 1e-9 && abs(noisy.threshold - 9) < 1e-9)
    }

    @Test func tooFewSamplesMakeNoBaseline() {
        #expect(MagneticBaseline(magnitudes: [Double](repeating: 47, count: 19)) == nil)
        #expect(MagneticBaseline(magnitudes: []) == nil)
    }

    @Test func currentIsDetectedOnlyBeyondTheThreshold() throws {
        let quiet = try #require(MagneticBaseline(magnitudes: [Double](repeating: 47, count: 100)))
        let on = try #require(Magnetometer.delta(baseline: quiet, recent: [Double](repeating: 52.1, count: 50)))
        let off = try #require(Magnetometer.delta(baseline: quiet, recent: [Double](repeating: 51.9, count: 50)))
        #expect(Magnetometer.state(delta: on, baseline: quiet) == .current)
        #expect(Magnetometer.state(delta: off, baseline: quiet) == ValueState.none)

        let noisy = try #require(MagneticBaseline(magnitudes: (0..<100).map { $0 % 2 == 0 ? 44.0 : 50.0 }))
        // 5 microtesla off the baseline is not enough when the phone's own noise is 3
        let small = try #require(Magnetometer.delta(baseline: noisy, recent: [52, 52, 52]))
        #expect(abs(small - 5) < 1e-9 && Magnetometer.state(delta: small, baseline: noisy) == ValueState.none)
        let big = try #require(Magnetometer.delta(baseline: noisy, recent: [60, 60]))
        #expect(Magnetometer.state(delta: big, baseline: noisy) == .current)
    }

    @Test func aDropInTheFieldCountsAsMuchAsARise() throws {
        let quiet = try #require(MagneticBaseline(magnitudes: [Double](repeating: 47, count: 100)))
        #expect(abs(try #require(Magnetometer.delta(baseline: quiet, recent: [40, 40])) - 7) < 1e-9)
        #expect(Magnetometer.delta(baseline: quiet, recent: []) == nil)
    }

    @Test func fieldStrengthIsTheLengthOfTheThreeAxes() {
        #expect(Magnetometer.magnitude(x: 3, y: 4, z: 12) == 13)
    }

    // MARK: Relative light

    @Test func halfTheExposureForTheSamePictureMeansTwiceTheLight() throws {
        let a = try #require(RelativeLight.level(meanBrightness: 0.5, iso: 100, exposureS: 1.0 / 60))
        let b = try #require(RelativeLight.level(meanBrightness: 0.5, iso: 100, exposureS: 1.0 / 120))
        #expect(abs(b / a - 2) < 1e-9)
        #expect(RelativeLight.level(meanBrightness: 0.5, iso: 0, exposureS: 0.01) == nil)
        #expect(RelativeLight.level(meanBrightness: 0, iso: 100, exposureS: 0.01) == nil)
    }

    @Test func theRatioToTheBaselineSaysOffDimOrOn() {
        #expect(RelativeLight.state(ratio: 1.0) == .off && RelativeLight.state(ratio: 1.29) == .off)
        #expect(RelativeLight.state(ratio: 1.3) == .dim && RelativeLight.state(ratio: 3.99) == .dim)
        #expect(RelativeLight.state(ratio: 4) == .on && RelativeLight.state(ratio: 200) == .on)
    }

    // MARK: Readings

    private func json(_ r: MeterReading) throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: JSON.encoder.encode(r)) as? [String: Any])
    }

    @Test func aMagnetometerReadingNamesThePartAndHasNoNet() throws {
        let r = PhoneReadings.reading(kind: .magnetometer, part: " M1 ", value: .magnetometer(deltaUT: 42.5, state: .current),
                                      actionID: "chk_1", scanID: "scan_1", sessionID: "sess_1", now: Date(timeIntervalSince1970: 1_800_000_000), id: "rd_m")
        let j = try json(r)
        #expect(j["kind"] as? String == "magnetometer" && j["source"] as? String == "phone" && j["part"] as? String == "M1")
        #expect(j["net"] == nil && j["pins"] == nil && j["meter"] == nil && j["power_state"] as? String == "on")
        let v = try #require(j["value"] as? [String: Any])
        #expect(v["delta_uT"] as? Double == 42.5 && v["state"] as? String == "current" && v.count == 2)
    }

    @Test func eachSensorKindEncodesToItsOwnShape() throws {
        func value(_ v: Value, _ kind: MeterReadingKind) throws -> [String: Any] {
            let r = PhoneReadings.reading(kind: kind, part: "P", value: v, actionID: nil, scanID: "s", sessionID: "x", id: "rd")
            return try #require(try json(r)["value"] as? [String: Any])
        }
        let audio = try value(.audio(hz: 187.5), .audio)
        #expect(audio["hz"] as? Double == 187.5 && audio.count == 1)
        let flicker = try value(.flicker(hz: 120, depth: 0.35), .flicker)
        #expect(flicker["hz"] as? Double == 120 && flicker["depth"] as? Double == 0.35 && flicker.count == 2)
        let light = try value(.light(state: .dim), .light)
        #expect(light["state"] as? String == "dim" && light.count == 1)
        let led = try value(.led(state: .blinking, color: "green"), .led)
        #expect(led["state"] as? String == "blinking" && led["color"] as? String == "green" && led.count == 2)
        let plain = try value(.led(state: .off, color: nil), .led)
        #expect(plain["color"] == nil)
    }

    @Test func valuesAreRoundedToWhatTheSensorsCanTell() {
        #expect(PhoneReadings.rounded(187.4999, places: 1) == 187.5)
        #expect(PhoneReadings.rounded(0.3456, places: 2) == 0.35)
    }

    @Test func theCameraAndMicrophoneRefusalsAreSaidInPlainWords() {
        #expect(PhoneCheckWords.camera(CameraProbeError.denied).contains("Settings > Privacy & Security > Camera"))
        #expect(PhoneCheckWords.camera(CameraProbeError.cannotStart).hasPrefix("The camera did not start"))
        #expect(PhoneCheckWords.microphoneDenied.contains("Microphone"))
    }
}
