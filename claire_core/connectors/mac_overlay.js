// A blue frame and label over the screen while Claire is controlling an app,
// so it is never a surprise. Clicks pass straight through it.
ObjC.import('Cocoa');
function run(argv) {
  // Keep the display awake only for this visible control lease. -w releases
  // the assertion even if the overlay is killed or Claire crashes.
  const awake = $.NSTask.alloc.init;
  awake.launchPath = '/usr/bin/caffeinate';
  awake.arguments = ['-d', '-i', '-u', '-w', String($.NSProcessInfo.processInfo.processIdentifier)];
  awake.standardOutput = $.NSFileHandle.fileHandleWithNullDevice;
  awake.standardError = $.NSFileHandle.fileHandleWithNullDevice;
  try { awake.launch; } catch (e) {}
  const app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
  const screens = $.NSScreen.screens;
  for (let i = 0; i < screens.count; i++) {
    const f = screens.objectAtIndex(i).frame;
    const w = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
      f, $.NSWindowStyleMaskBorderless, $.NSBackingStoreBuffered, false);
    w.setOpaque(false);
    w.setHasShadow(false);
    w.setIgnoresMouseEvents(true);
    w.setLevel($.NSScreenSaverWindowLevel);
    w.setCollectionBehavior($.NSWindowCollectionBehaviorCanJoinAllSpaces |
                            $.NSWindowCollectionBehaviorStationary |
                            $.NSWindowCollectionBehaviorFullScreenAuxiliary);
    w.setBackgroundColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.25, 0.60, 1.0, 0.07));
    const v = w.contentView;
    v.setWantsLayer(true);
    v.layer.setBorderWidth(5);
    v.layer.setBorderColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.30, 0.65, 1.0, 0.95).CGColor);
    const text = $.NSTextField.labelWithString($(argv[0] || 'Claire is controlling this Mac'));
    text.setFont($.NSFont.boldSystemFontOfSize(13));
    text.setTextColor($.NSColor.whiteColor);
    text.setDrawsBackground(true);
    text.setBackgroundColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.10, 0.40, 0.90, 0.95));
    text.sizeToFit;
    const tf = text.frame;
    text.setFrameOrigin($.NSMakePoint((f.size.width - tf.size.width) / 2, f.size.height - tf.size.height - 8));
    v.addSubview(text);
    w.orderFrontRegardless;
  }
  app.run;
}
