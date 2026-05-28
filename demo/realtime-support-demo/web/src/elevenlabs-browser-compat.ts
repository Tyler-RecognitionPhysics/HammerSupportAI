/**
 * @elevenlabs/client requests `voiceIsolation: true` in getUserMedia and queries
 * the Permissions API for microphone state. Both throw NotSupportedError on many
 * browsers (including headless Chromium and some production Chrome builds).
 * Retry without unsupported constraints so browser voice works on Vercel.
 */
export function installElevenLabsBrowserCompatShims(): void {
  if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) return;

  const mediaDevices = navigator.mediaDevices;
  const origGetUserMedia = mediaDevices.getUserMedia.bind(mediaDevices);
  mediaDevices.getUserMedia = async function patchedGetUserMedia(constraints) {
    try {
      return await origGetUserMedia(constraints);
    } catch (err) {
      const audio =
        constraints && typeof constraints === "object"
          ? (constraints as MediaStreamConstraints).audio
          : undefined;
      if (
        err instanceof DOMException &&
        err.name === "NotSupportedError" &&
        audio &&
        typeof audio === "object" &&
        !Array.isArray(audio) &&
        "voiceIsolation" in audio
      ) {
        const { voiceIsolation: _removed, ...rest } = audio as MediaTrackConstraints & {
          voiceIsolation?: boolean;
        };
        return origGetUserMedia({ ...(constraints as MediaStreamConstraints), audio: rest });
      }
      throw err;
    }
  };

  const permissions = navigator.permissions;
  if (!permissions?.query) return;

  const origQuery = permissions.query.bind(permissions);
  permissions.query = async function patchedPermissionsQuery(desc) {
    try {
      return await origQuery(desc);
    } catch (err) {
      if (
        err instanceof DOMException &&
        err.name === "NotSupportedError" &&
        desc &&
        typeof desc === "object" &&
        "name" in desc &&
        (desc as PermissionDescriptor).name === "microphone"
      ) {
        return { state: "granted", onchange: null } as PermissionStatus;
      }
      throw err;
    }
  };
}
