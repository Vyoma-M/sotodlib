import so3g.proj
import numpy as np
from pixell import enmap


DEG = np.pi/180

def _find_field(axisman, default, provided):
    """This is a utility function for the pattern where a default should
    be extracted from an AxisManager, unless an alternative key name
    has been passed in, or simply an alternative array of values.

    """
    if provided is None:
        provided = default
    if isinstance(provided, str):
        return axisman[provided]
    return provided

def get_radec(tod, wrap=False, dets=None, timestamps=None, focal_plane=None,
              boresight=None):
    dets = _find_field(tod, tod.dets.vals, dets)
    timestamps = _find_field(tod, 'timestamps', timestamps)
    boresight = _find_field(tod, 'boresight', boresight)
    fp = _find_field(tod, 'focal_plane', focal_plane)
    sight = so3g.proj.CelestialSightLine.az_el(
        timestamps, boresight.az, boresight.el, roll=boresight.roll,
        site='so', weather='typical')
    fp = so3g.proj.FocalPlane.from_xieta(dets, fp.xi, fp.eta, fp.gamma)
    asm = so3g.proj.Assembly.attach(sight, fp)
    output = np.zeros((len(dets), len(timestamps), 4))
    proj = so3g.proj.Projectionist()
    proj.get_coords(asm, output=output)
    if wrap is True:
        wrap = 'radec'
    if wrap:
        tod.wrap(wrap, output, [(0, 'dets'), (1, 'samps')])
    return output

def get_horiz(tod, wrap=False, dets=None, timestamps=None, focal_plane=None,
              boresight=None):
    dets = _find_field(tod, tod.dets.vals, dets)
    timestamps = _find_field(tod, 'timestamps', timestamps)
    boresight = _find_field(tod, 'boresight', boresight)
    fp = _find_field(tod, 'focal_plane', focal_plane)

    sight = so3g.proj.CelestialSightLine.for_horizon(
        timestamps, boresight.az, boresight.el, roll=boresight.roll)

    fp = so3g.proj.FocalPlane.from_xieta(dets, fp.xi, fp.eta, fp.gamma)
    asm = so3g.proj.Assembly.attach(sight, fp)
    output = np.zeros((len(dets), len(timestamps), 4))
    proj = so3g.proj.Projectionist()
    proj.get_coords(asm, output=output)
    # The lonlat pair is (-az, el), so restore the az sign.
    output[:,:,0] *= -1
    if wrap is True:
        wrap = 'horiz'
    if wrap:
        tod.wrap(wrap, output, [(0, 'dets'), (1, 'samps')])
    return output

def get_wcs_kernel(proj, ra, dec, res):
    """Get a WCS "kernel" -- this is a WCS holding a single pixel
    centered on CRVAL.

    This interface _will_ change.

    """
    assert np.isscalar(res)  # This ain't enlib.
    _, wcs = enmap.geometry(np.array((dec, ra)), shape=(1,1), proj=proj,
                            res=(res, -res))
    return wcs

def get_footprint(tod, wcs_kernel, dets=None, timestamps=None, boresight=None,
                  focal_plane=None):
    """Find a geometry (in the sense of enmap) based on wcs_kernel that is
    big enough to contain all data from tod.  Returns (shape, wcs).

    """
    dets = _find_field(tod, tod.dets.vals, dets)
    timestamps = _find_field(tod, 'timestamps', timestamps)
    boresight = _find_field(tod, 'boresight', boresight)
    fp0 = _find_field(tod, 'focal_plane', focal_plane)

    # Do a simplest convex hull...
    q = so3g.proj.quat.rotation_xieta(fp0.xi, fp0.eta)
    xi, eta, _ = so3g.proj.quat.decompose_xieta(q)
    xi0, eta0 = xi.mean(), eta.mean()
    R = ((xi - xi0)**2 + (eta - eta0)**2).max()**.5

    n_circ = 16
    dphi = 2*np.pi/n_circ
    phi = np.arange(n_circ) * dphi
    L = 1.01 * R / np.cos(dphi/2)
    xi, eta = L * np.cos(phi) + xi0, L * np.sin(phi) + eta0
    fake_dets = ['hull%i' % i for i in range(n_circ)]
    fp1 = so3g.proj.FocalPlane.from_xieta(fake_dets, xi, eta, 0*xi)

    sight = so3g.proj.CelestialSightLine.az_el(
        timestamps, boresight.az, boresight.el, roll=boresight.roll,
        site='so', weather='typical')
    asm = so3g.proj.Assembly.attach(sight, fp1)
    output = np.zeros((len(fake_dets), len(timestamps), 4))
    proj = so3g.proj.Projectionist.for_geom((1,1), wcs_kernel)
    proj.get_planar(asm, output=output)

    output2 = output*0
    proj.get_coords(asm, output=output2)

    delts = wcs_kernel.wcs.cdelt * DEG
    planar = [output[:,:,0], output[:,:,1]]
    # Get the extrema..
    ranges = [(p.min()/d, p.max()/d) for p, d in zip(planar, delts)]
    del output
    
    # Start a new WCS and set the lower left corner.
    w = wcs_kernel.copy()
    corner = [int(np.floor(min(r)+.5)) for r in ranges]
    w.wcs.crpix = [1 - corner[0], 1 - corner[1]]

    # The other corner helps us with the shape...
    far_corner = [int(np.floor(max(r)+.5)) for r in ranges]
    shape = tuple([fc - c + 1 for fc, c in zip(far_corner, corner)][::-1])
    return (shape, w)