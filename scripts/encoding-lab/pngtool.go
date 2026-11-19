// Command pngtool is decision-support tooling for the prism-plate encoding
// upgrade evaluation. It decodes, re-encodes, diffs and hashes PNG files
// using only the Go standard library (the same image/png the service uses).
//
// Usage:
//
//	pngtool pixels   a.png                 -> decode + pixel hash + timing
//	pngtool reencode a.png b.png           -> re-encode at BestCompression, verify pixel equality
//	pngtool diff     a.png b.png [tol]     -> exact RGBA diff + composited(visual) diff at tolerance
package main

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"os"
	"strconv"
	"time"
)

func load(path string) (*image.NRGBA, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	img, err := png.Decode(f)
	if err != nil {
		return nil, err
	}
	if n, ok := img.(*image.NRGBA); ok {
		return n, nil
	}
	b := img.Bounds()
	n := image.NewNRGBA(b)
	for y := b.Min.Y; y < b.Max.Y; y++ {
		for x := b.Min.X; x < b.Max.X; x++ {
			n.Set(x, y, img.At(x, y))
		}
	}
	return n, nil
}

func pixelHash(n *image.NRGBA) string {
	h := sha256.New()
	var dim [8]byte
	binary.BigEndian.PutUint32(dim[0:4], uint32(n.Bounds().Dx()))
	binary.BigEndian.PutUint32(dim[4:8], uint32(n.Bounds().Dy()))
	h.Write(dim[:])
	h.Write(n.Pix)
	return fmt.Sprintf("%x", h.Sum(nil))
}

func out(v any) {
	b, _ := json.Marshal(v)
	fmt.Println(string(b))
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "usage: pngtool <pixels|reencode|diff> ...")
		os.Exit(2)
	}
	switch os.Args[1] {
	case "pixels":
		t0 := time.Now()
		n, err := load(os.Args[2])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		st, _ := os.Stat(os.Args[2])
		out(map[string]any{
			"file": os.Args[2], "width": n.Bounds().Dx(), "height": n.Bounds().Dy(),
			"png_bytes": st.Size(), "pixel_hash": pixelHash(n), "decode_ns": time.Since(t0).Nanoseconds(),
		})
	case "reencode":
		t0 := time.Now()
		n, err := load(os.Args[2])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		decodeNS := time.Since(t0).Nanoseconds()
		t1 := time.Now()
		f, err := os.Create(os.Args[3])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		enc := png.Encoder{CompressionLevel: png.BestCompression}
		if err := enc.Encode(f, n); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		f.Close()
		encodeNS := time.Since(t1).Nanoseconds()
		// Verify the round trip is pixel-exact.
		m, err := load(os.Args[3])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		sa, _ := os.Stat(os.Args[2])
		sb, _ := os.Stat(os.Args[3])
		out(map[string]any{
			"src": os.Args[2], "dst": os.Args[3],
			"src_bytes": sa.Size(), "dst_bytes": sb.Size(),
			"pixel_equal": pixelHash(n) == pixelHash(m),
			"pixel_hash":  pixelHash(m),
			"decode_ns":   decodeNS, "encode_ns": encodeNS,
		})
	case "diff":
		tol := 0
		if len(os.Args) > 4 {
			tol, _ = strconv.Atoi(os.Args[4])
		}
		a, err := load(os.Args[2])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		b, err := load(os.Args[3])
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		if !a.Bounds().Eq(b.Bounds()) {
			fmt.Fprintln(os.Stderr, "bounds differ")
			os.Exit(1)
		}
		exactDiff, maxDelta, visualDiff := 0, 0, 0
		for i := 0; i < len(a.Pix); i += 4 {
			same := true
			visSame := true
			for c := 0; c < 4; c++ {
				d := int(a.Pix[i+c]) - int(b.Pix[i+c])
				if d < 0 {
					d = -d
				}
				if d > 0 {
					same = false
				}
				if d > maxDelta {
					maxDelta = d
				}
			}
			// Composite both pixels over opaque white, then compare with tolerance.
			for c := 0; c < 3; c++ {
				fa := float64(a.Pix[i+3]) / 255
				fb := float64(b.Pix[i+3]) / 255
				va := float64(a.Pix[i+c])*fa + 255*(1-fa)
				vb := float64(b.Pix[i+c])*fb + 255*(1-fb)
				d := va - vb
				if d < 0 {
					d = -d
				}
				if d > float64(tol)+1e-9 {
					visSame = false
				}
			}
			if !same {
				exactDiff++
			}
			if !visSame {
				visualDiff++
			}
		}
		out(map[string]any{
			"a": os.Args[2], "b": os.Args[3],
			"pixel_equal":       exactDiff == 0,
			"exact_diff_pixels": exactDiff,
			"max_channel_delta": maxDelta,
			"visual_tolerance":  tol,
			"visual_diff_pixels": visualDiff,
			"pixel_hash_a":      pixelHash(a),
			"pixel_hash_b":      pixelHash(b),
		})
	default:
		fmt.Fprintln(os.Stderr, "unknown command")
		os.Exit(2)
	}
	_ = color.NRGBA{}
}
