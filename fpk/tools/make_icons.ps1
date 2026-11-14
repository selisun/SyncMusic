# Regenerate all fnOS icon sizes from tools/icon_source.png (user-provided artwork).
# The source artwork has an opaque near-black background; this script flood-fills it
# from the image border into transparent pixels first, so icons have no black corners.
# Requires Windows PowerShell 5+ (System.Drawing). ASCII only.
# Usage: powershell -ExecutionPolicy Bypass -File make_icons.ps1 [threshold] [outRoot]
param([int]$Threshold = 45, [string]$OutRoot = '')

Add-Type -AssemblyName System.Drawing
$cs = @'
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

public static class IconTool
{
    public static double LastMaskedPct = 0;

    public static Bitmap RemoveDarkCornersBitmap(string srcPath, int threshold)
    {
        Bitmap bmp;
        {
            using (Bitmap loaded = new Bitmap(srcPath))
            {
                int w = loaded.Width, h = loaded.Height;
                bmp = new Bitmap(w, h, PixelFormat.Format32bppArgb);
                using (Graphics g = Graphics.FromImage(bmp))
                {
                    g.CompositingMode = System.Drawing.Drawing2D.CompositingMode.SourceCopy;
                    g.DrawImage(loaded, 0, 0, w, h);
                }
                BitmapData bd = bmp.LockBits(new Rectangle(0, 0, w, h), ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
                int stride = bd.Stride;
                byte[] buf = new byte[stride * h];
                Marshal.Copy(bd.Scan0, buf, 0, buf.Length);

                bool[] mask = new bool[w * h];
                Queue<int> q = new Queue<int>();
                for (int x = 0; x < w; x++) { Seed(buf, stride, mask, q, w, x, 0, threshold); Seed(buf, stride, mask, q, w, x, h - 1, threshold); }
                for (int y = 0; y < h; y++) { Seed(buf, stride, mask, q, w, 0, y, threshold); Seed(buf, stride, mask, q, w, w - 1, y, threshold); }
                int masked = 0;
                while (q.Count > 0)
                {
                    int idx = q.Dequeue();
                    masked++;
                    int x = idx % w, y = idx / w;
                    if (x > 0) Seed(buf, stride, mask, q, w, x - 1, y, threshold);
                    if (x < w - 1) Seed(buf, stride, mask, q, w, x + 1, y, threshold);
                    if (y > 0) Seed(buf, stride, mask, q, w, x, y - 1, threshold);
                    if (y < h - 1) Seed(buf, stride, mask, q, w, x, y + 1, threshold);
                }

                // Spread foreground colours into the masked area so bicubic scaling cannot
                // blend transparent black into the artwork edge (no dark halo).
                byte[] rgb = new byte[w * h * 3];
                bool[] known = new bool[w * h];
                for (int y = 0; y < h; y++)
                    for (int x = 0; x < w; x++)
                    {
                        int i = y * w + x, o = y * stride + x * 4;
                        if (!mask[i]) { known[i] = true; rgb[i * 3] = buf[o + 2]; rgb[i * 3 + 1] = buf[o + 1]; rgb[i * 3 + 2] = buf[o]; }
                    }
                for (int pass = 0; pass < 6; pass++)
                {
                    bool[] nk = (bool[])known.Clone();
                    byte[] nrgb = (byte[])rgb.Clone();
                    for (int y = 0; y < h; y++)
                        for (int x = 0; x < w; x++)
                        {
                            int i = y * w + x;
                            if (known[i] || !mask[i]) continue;
                            int rs = 0, gs = 0, bs = 0, n = 0;
                            for (int dy = -1; dy <= 1; dy++)
                                for (int dx = -1; dx <= 1; dx++)
                                {
                                    int nx = x + dx, ny = y + dy;
                                    if (nx < 0 || ny < 0 || nx >= w || ny >= h) continue;
                                    int j = ny * w + nx;
                                    if (!known[j]) continue;
                                    rs += rgb[j * 3]; gs += rgb[j * 3 + 1]; bs += rgb[j * 3 + 2]; n++;
                                }
                            if (n > 0) { nrgb[i * 3] = (byte)(rs / n); nrgb[i * 3 + 1] = (byte)(gs / n); nrgb[i * 3 + 2] = (byte)(bs / n); nk[i] = true; }
                        }
                    known = nk; rgb = nrgb;
                }

                for (int y = 0; y < h; y++)
                    for (int x = 0; x < w; x++)
                    {
                        int i = y * w + x;
                        if (!mask[i]) continue;
                        int o = y * stride + x * 4;
                        buf[o] = rgb[i * 3 + 2]; buf[o + 1] = rgb[i * 3 + 1]; buf[o + 2] = rgb[i * 3];
                        buf[o + 3] = 0;
                    }
                Marshal.Copy(buf, 0, bd.Scan0, buf.Length);
                bmp.UnlockBits(bd);
                LastMaskedPct = (double)masked / (w * h) * 100.0;
            }
        }
        return bmp;
    }

    static void Seed(byte[] buf, int stride, bool[] mask, Queue<int> q, int w, int x, int y, int threshold)
    {
        int i = y * w + x;
        if (mask[i]) return;
        int o = y * stride + x * 4;
        int b = buf[o], g = buf[o + 1], r = buf[o + 2], a = buf[o + 3];
        if (a == 0 || Math.Max(r, Math.Max(g, b)) <= threshold) { mask[i] = true; q.Enqueue(i); }
    }

    public static string CornerAlphas(string path)
    {
        using (Bitmap b = new Bitmap(path))
        {
            return string.Format("alpha corners L-T={0} R-T={1} L-B={2} R-B={3}",
                b.GetPixel(0, 0).A, b.GetPixel(b.Width - 1, 0).A,
                b.GetPixel(0, b.Height - 1).A, b.GetPixel(b.Width - 1, b.Height - 1).A);
        }
    }
}
'@
Add-Type -TypeDefinition $cs -ReferencedAssemblies System.Drawing

$root = Split-Path -Parent $PSScriptRoot
if ($OutRoot -ne '') { $root = $OutRoot }
New-Item -ItemType Directory -Force -Path (Join-Path $root 'app\ui\images') | Out-Null
$srcPath = Join-Path $PSScriptRoot 'icon_source.png'
if (-not (Test-Path $srcPath)) { throw "missing $srcPath" }

$src = [IconTool]::RemoveDarkCornersBitmap($srcPath, $Threshold)
Write-Output ("background removed: {0:N2}% of pixels (threshold {1})" -f [IconTool]::LastMaskedPct, $Threshold)

$attr = New-Object System.Drawing.Imaging.ImageAttributes
$attr.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
try {
  $jobs = @()
  foreach ($s in 16,24,32,48,64,72,96,128,256) { $jobs += ,@($s, ("app\ui\images\icon_{0}.png" -f $s)) }
  foreach ($p in @(@(64, 'ICON.PNG'), @(256, 'ICON_256.PNG'))) { $jobs += ,@($p[0], $p[1]) }
  foreach ($job in $jobs) {
    $size = $job[0]; $out = Join-Path $root $job[1]
    $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.Clear([System.Drawing.Color]::Transparent)
    $rect = New-Object System.Drawing.Rectangle(0, 0, $size, $size)
    $g.DrawImage($src, $rect, 0, 0, $src.Width, $src.Height, [System.Drawing.GraphicsUnit]::Pixel, $attr)
    $g.Dispose()
    $ms = New-Object System.IO.MemoryStream
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    [System.IO.File]::WriteAllBytes($out, $ms.ToArray())
    $ms.Dispose()
    Write-Output ("{0} OK  {1}" -f $job[1], [IconTool]::CornerAlphas($out))
  }
} finally { $src.Dispose(); $attr.Dispose() }
