import AVFoundation
import CoreImage
import ImageIO
import Foundation

func log(_ text: String) { FileHandle.standardError.write(Data((text + "\n").utf8)) }
let args = CommandLine.arguments
let authorizeOnly = args.contains("--authorize")
let cameraName = args.count > 1 ? args[1] : "HD33 video"
let dimensions = (args.count > 2 ? args[2] : "1280x720").split(separator: "x").compactMap { Int32($0) }
let fps = args.count > 3 ? Double(args[3]) ?? 30 : 30
if dimensions.count != 2 || fps < 1 || fps > 60 { log("Invalid resolution/fps"); exit(2) }

final class Capture: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let context = CIContext(options: [.cacheIntermediates: false])
    let output = AVCaptureVideoDataOutput()
    var previous = -Double.infinity
    var frameCount = 0

    func start() throws {
        guard let device = AVCaptureDevice.devices(for: .video).first(where: { $0.localizedName == cameraName }) else {
            throw NSError(domain: "KVM", code: 1, userInfo: [NSLocalizedDescriptionKey: "Camera not found: \(cameraName)"])
        }
        session.beginConfiguration()
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { throw NSError(domain: "KVM", code: 2) }
        session.addInput(input)
        guard let format = device.formats.first(where: { f in
            let size = CMVideoFormatDescriptionGetDimensions(f.formatDescription)
            return size.width == dimensions[0] && size.height == dimensions[1] &&
                f.videoSupportedFrameRateRanges.contains(where: { $0.minFrameRate <= fps && $0.maxFrameRate >= fps })
        }) else { throw NSError(domain: "KVM", code: 3, userInfo: [NSLocalizedDescriptionKey: "Unsupported camera resolution/fps"]) }
        try device.lockForConfiguration()
        device.activeFormat = format
        let duration = CMTime(value: 1, timescale: Int32(fps))
        device.activeVideoMinFrameDuration = duration
        device.activeVideoMaxFrameDuration = duration
        device.unlockForConfiguration()
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.setSampleBufferDelegate(self, queue: DispatchQueue(label: "kvm.capture", qos: .userInteractive))
        guard session.canAddOutput(output) else { throw NSError(domain: "KVM", code: 4) }
        session.addOutput(output)
        session.commitConfiguration()
        session.startRunning()
        log("Capturing \(device.localizedName) \(dimensions[0])x\(dimensions[1]) at \(fps) fps")
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        autoreleasepool {
            let timestamp = CMTimeGetSeconds(CMSampleBufferGetPresentationTimeStamp(sampleBuffer))
            guard timestamp - previous >= 1.0 / fps - 0.002,
                  let buffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
            previous = timestamp
            let image = CIImage(cvPixelBuffer: buffer)
            guard let cg = context.createCGImage(image, from: image.extent),
                  let data = CFDataCreateMutable(nil, 0),
                  let destination = CGImageDestinationCreateWithData(data, "public.jpeg" as CFString, 1, nil) else { return }
            CGImageDestinationAddImage(destination, cg, [kCGImageDestinationLossyCompressionQuality: 0.72] as CFDictionary)
            guard CGImageDestinationFinalize(destination) else { return }
            FileHandle.standardOutput.write(data as Data)
            frameCount += 1
            if frameCount == 1 { log("First video frame delivered") }
        }
    }
}

let capture = Capture()
func startCapture() {
    DispatchQueue.global(qos: .userInitiated).async {
        do { try capture.start() }
        catch { log("Capture failed: \(error.localizedDescription)"); exit(1) }
    }
}
switch AVCaptureDevice.authorizationStatus(for: .video) {
case .authorized:
    if authorizeOnly { exit(0) }
    startCapture()
case .notDetermined:
    log("Camera permission required: allow Remote Desk Capture in the macOS camera prompt")
    AVCaptureDevice.requestAccess(for: .video) { granted in
        if granted {
            if authorizeOnly { exit(0) }
            startCapture()
        }
        else { log("Camera permission denied; enable Remote Desk Capture in Privacy & Security > Camera"); exit(3) }
    }
default:
    log("Camera permission denied; enable Remote Desk Capture in Privacy & Security > Camera")
    exit(3)
}
RunLoop.main.run()
