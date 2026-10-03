/* Shooting dates are calendar strings, never converted through Date/UTC. */
(function (root) {
  const metadata = {
    year(photo) { return photo.shotDate ? photo.shotDate.slice(0, 4) : String(photo.year); },
    sort(photos) {
      return [...photos].sort((a, b) => {
        if (a.shotDate && b.shotDate) return a.shotDate.localeCompare(b.shotDate);
        if (a.shotDate) return -1;
        if (b.shotDate) return 1;
        return 0; // Keep the existing order for undated photos.
      });
    }
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = metadata;
  else root.PhotoMetadata = metadata;
})(globalThis);
