// imgtool assembles the family images without a Docker daemon, so a 40 GB image
// never has to fit on a CI runner's disk twice over (buildx keeps both the layer
// snapshot and its compressed blob; that is what made baking models impossible on
// a stock GitHub runner).
//
//	imgtool has      -image REF -dest PATH
//	    exit 0 if REF already carries a model layer for PATH, 1 if not.
//	imgtool add      -image REF -file LOCAL -dest PATH
//	    append LOCAL as one layer at PATH to REF (an empty image if REF does not
//	    exist yet) and push it back to REF. One model file = one layer.
//	imgtool assemble -base REF -models REF -out REF[,REF...]
//	    push base + every layer of models to each out ref. Layers already in the
//	    registry are not re-uploaded, so this takes seconds, not an hour.
//
// Auth comes from the docker config that docker/login-action writes.
package main

import (
	"archive/tar"
	"compress/gzip"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"strings"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
	"github.com/google/go-containerregistry/pkg/v1/tarball"
	"github.com/google/go-containerregistry/pkg/v1/types"
)

// Every history entry for a model layer is tagged with this prefix + its path,
// which is how `has` finds it again.
const historyPrefix = "imgtool model "

var opts = []remote.Option{remote.WithAuthFromKeychain(authn.DefaultKeychain)}

func main() {
	if len(os.Args) < 2 {
		log.Fatal("usage: imgtool has|add|assemble ...")
	}
	fs := flag.NewFlagSet(os.Args[1], flag.ExitOnError)
	image := fs.String("image", "", "image ref")
	dest := fs.String("dest", "", "absolute path of the file inside the image")
	file := fs.String("file", "", "local file to add")
	base := fs.String("base", "", "code image ref")
	models := fs.String("models", "", "models image ref")
	out := fs.String("out", "", "comma-separated output refs")
	_ = fs.Parse(os.Args[2:])

	var err error
	switch os.Args[1] {
	case "has":
		var ok bool
		ok, err = has(*image, *dest)
		if err == nil && !ok {
			os.Exit(1)
		}
	case "add":
		err = add(*image, *file, *dest)
	case "assemble":
		err = assemble(*base, *models, strings.Split(*out, ","))
	default:
		err = fmt.Errorf("unknown command %q", os.Args[1])
	}
	if err != nil {
		log.Fatal(err)
	}
}

// load returns the image at ref, or an empty OCI image when ref does not exist.
func load(ref string) (v1.Image, name.Reference, error) {
	r, err := name.ParseReference(ref)
	if err != nil {
		return nil, nil, err
	}
	img, err := remote.Image(r, opts...)
	var terr *transport.Error
	if errors.As(err, &terr) && terr.StatusCode == http.StatusNotFound {
		img = mutate.MediaType(empty.Image, types.OCIManifestSchema1)
		img = mutate.ConfigMediaType(img, types.OCIConfigJSON)
		return img, r, nil
	}
	return img, r, err
}

func has(ref, dest string) (bool, error) {
	img, _, err := load(ref)
	if err != nil {
		return false, err
	}
	cf, err := img.ConfigFile()
	if err != nil {
		return false, err
	}
	for _, h := range cf.History {
		if h.CreatedBy == historyPrefix+dest {
			return true, nil
		}
	}
	return false, nil
}

// tarOpener streams a tar holding just `file` at `dest` (plus its parent dirs),
// with fixed metadata so the same file always yields the same layer digest.
func tarOpener(file, dest string) tarball.Opener {
	return func() (io.ReadCloser, error) {
		f, err := os.Open(file)
		if err != nil {
			return nil, err
		}
		st, err := f.Stat()
		if err != nil {
			f.Close()
			return nil, err
		}
		pr, pw := io.Pipe()
		go func() {
			defer f.Close()
			tw := tar.NewWriter(pw)
			epoch := time.Unix(0, 0)
			rel := strings.TrimPrefix(path.Clean(dest), "/")
			parts := strings.Split(rel, "/")
			for i := 1; i < len(parts); i++ {
				h := &tar.Header{Typeflag: tar.TypeDir, Name: strings.Join(parts[:i], "/") + "/",
					Mode: 0o755, ModTime: epoch, Format: tar.FormatPAX}
				if err := tw.WriteHeader(h); err != nil {
					pw.CloseWithError(err)
					return
				}
			}
			h := &tar.Header{Typeflag: tar.TypeReg, Name: rel, Mode: 0o644, Size: st.Size(),
				ModTime: epoch, Format: tar.FormatPAX}
			if err := tw.WriteHeader(h); err != nil {
				pw.CloseWithError(err)
				return
			}
			if _, err := io.Copy(tw, f); err != nil {
				pw.CloseWithError(err)
				return
			}
			pw.CloseWithError(tw.Close())
		}()
		return pr, nil
	}
}

func add(ref, file, dest string) error {
	if ok, err := has(ref, dest); err != nil {
		return err
	} else if ok {
		log.Printf("%s already in %s, skipping", dest, ref)
		return nil
	}
	img, r, err := load(ref)
	if err != nil {
		return err
	}
	layer, err := tarball.LayerFromOpener(tarOpener(file, dest),
		tarball.WithCompressionLevel(gzip.BestSpeed),
		tarball.WithMediaType(types.OCILayer))
	if err != nil {
		return err
	}
	img, err = mutate.Append(img, mutate.Addendum{
		Layer:     layer,
		MediaType: types.OCILayer,
		History:   v1.History{CreatedBy: historyPrefix + dest, Created: v1.Time{Time: time.Unix(0, 0)}},
	})
	if err != nil {
		return err
	}
	start := time.Now()
	log.Printf("pushing %s -> %s", dest, ref)
	if err := remote.Write(r, img, opts...); err != nil {
		return err
	}
	log.Printf("pushed %s in %s", dest, time.Since(start).Round(time.Second))
	return nil
}

func assemble(baseRef, modelsRef string, outs []string) error {
	b, err := name.ParseReference(baseRef)
	if err != nil {
		return err
	}
	base, err := remote.Image(b, opts...)
	if err != nil {
		return fmt.Errorf("base %s: %w", baseRef, err)
	}
	bmt, err := base.MediaType()
	if err != nil {
		return err
	}
	if bmt != types.OCIManifestSchema1 {
		return fmt.Errorf("base %s is %s, want an OCI manifest (build with oci-mediatypes=true)", baseRef, bmt)
	}
	m, err := name.ParseReference(modelsRef)
	if err != nil {
		return err
	}
	models, err := remote.Image(m, opts...)
	if err != nil {
		return fmt.Errorf("models %s: %w", modelsRef, err)
	}
	layers, err := models.Layers()
	if err != nil {
		return err
	}
	cf, err := models.ConfigFile()
	if err != nil {
		return err
	}
	var hist []v1.History
	for _, h := range cf.History {
		if !h.EmptyLayer {
			hist = append(hist, h)
		}
	}
	if len(hist) != len(layers) {
		return fmt.Errorf("models image has %d layers but %d history entries", len(layers), len(hist))
	}
	adds := make([]mutate.Addendum, len(layers))
	for i, l := range layers {
		adds[i] = mutate.Addendum{Layer: l, MediaType: types.OCILayer, History: hist[i]}
	}
	img, err := mutate.Append(base, adds...)
	if err != nil {
		return err
	}
	for _, o := range outs {
		r, err := name.ParseReference(strings.TrimSpace(o))
		if err != nil {
			return err
		}
		log.Printf("pushing %s", r)
		if err := remote.Write(r, img, opts...); err != nil {
			return err
		}
	}
	d, err := img.Digest()
	if err != nil {
		return err
	}
	fmt.Println(d)
	return nil
}
