// Trains Lea's "Hey Leah" sound classifier with Create ML (Apple's licensed on-device path).
// Data: <dir>/hey_leah/*.wav (the phrase, many takes, several people/rooms if possible)
//       <dir>/background/*.wav (room noise, other speech, TV, Lea's own voice)
// Run:  swift train.swift <dir> ../../MyApp/HeyLeah.mlmodel   (Xcode compiles it to HeyLeah.mlmodelc in the app)
// It prints held-out accuracy; check misses and false triggers on the iPhone before relying on it.
import CreateML
import Foundation

let args = CommandLine.arguments
guard args.count == 3 else { print("usage: swift train.swift <data-dir> <out.mlmodel>"); exit(64) }
let data = URL(fileURLWithPath: args[1], isDirectory: true)
for label in ["hey_leah", "background"] where !FileManager.default.fileExists(atPath: data.appendingPathComponent(label).path) {
    print("missing \(label)/ in \(data.path)"); exit(65)
}
let classifier = try MLSoundClassifier(trainingData: .labeledDirectories(at: data),
                                       parameters: .init(validation: .split(strategy: .automatic), overlapFactor: 0.5))
print("training accuracy: \(String(format: "%.1f", (1 - classifier.trainingMetrics.classificationError) * 100))%")
print("validation accuracy: \(String(format: "%.1f", (1 - classifier.validationMetrics.classificationError) * 100))%")
print(classifier.validationMetrics.confusion)
try classifier.write(to: URL(fileURLWithPath: args[2]), metadata: MLModelMetadata(author: "Lea", shortDescription: "Hey Leah wake phrase", version: "1"))
print("wrote \(args[2])")
