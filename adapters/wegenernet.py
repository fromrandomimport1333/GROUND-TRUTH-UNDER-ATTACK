from datetime import datetime,timezone
from pathlib import Path
import numpy as np
from netCDF4 import Dataset,num2date
from pyproj import Transformer
from adapters.dataset import Measurement
from core.contracts import Location

def number(v):
    return None if np.ma.is_masked(v) or not np.isfinite(v) else float(v)

def hours(paths,cells):
    if not cells or len(cells)>100 or len(set(cells))!=len(cells): raise ValueError('Select 1..100 unique cells')
    previous=None; grid=None
    transform=Transformer.from_crs(32633,4326,always_xy=True)
    for path in sorted(map(Path,paths)):
        with Dataset(path) as f:
            t=f['time']; v=f['T_zAvgTerr_HM']
            if v.dimensions!=('time','Y','X') or v.units!='K' or f['UTM33N'].epsg_code!='EPSG:32633': raise ValueError('Unsupported schema')
            xy=[]
            for y,x in cells:
                if not 0<=y<len(f.dimensions['Y']) or not 0<=x<len(f.dimensions['X']):raise ValueError('Cell outside grid')
                xy.append((float(f['X'][x]),float(f['Y'][y])))
            if grid is not None and grid!=xy:raise ValueError('Grid changed')
            grid=xy
            locations=[Location(longitude=float(a),latitude=float(b)) for a,b in (transform.transform(*p) for p in xy)]
            for i in range(len(t)):
                raw=num2date(t[i],t.units,calendar=t.calendar,only_use_cftime_datetimes=False)
                dt=datetime(raw.year,raw.month,raw.day,raw.hour,raw.minute,raw.second,tzinfo=timezone.utc)
                bounds=num2date(f['time_bnds'][i],f['time_bnds'].units,calendar=t.calendar,only_use_cftime_datetimes=False)
                if (bounds[1]-bounds[0]).total_seconds()!=3600 or bounds[1]!=raw:raise ValueError('Expected end-labelled hourly means')
                if previous is not None and (dt-previous).total_seconds()!=3600:raise ValueError('Duplicate, overlap or missing hour')
                previous=dt; batch=[]; evidence=[]
                for (y,x),loc in zip(cells,locations):
                    sid=f'wn_grid_y{y}_x{x}'
                    record=f'{path.name}:T_zAvgTerr_HM:{i}:{y}:{x}'
                    batch.append(Measurement(sensor_id=sid,observed_at=dt,value=number(v[i,y,x]),unit='K',location=loc,source_id='wegenernet_v8',source_record_id=record))
                    evidence.append(dict(sensor_id=sid,time=dt.isoformat(),source_record_id=record,interval_start=str(bounds[0]),interval_end=str(bounds[1]),flagged_t_percent=number(f['Flagged_T'][i,y,x])))
                yield dt,batch,evidence


# The legacy hours() temperature adapter remains supported unchanged.
import hashlib
import re
from datetime import timedelta
from dataclasses import dataclass

VARIABLES = {
    'T_zAvgTerr_HM': ('K', 'Flagged_T'),
    'T_zAvgTerr_HMin': ('K', 'Flagged_T'),
    'T_zAvgTerr_HMax': ('K', 'Flagged_T'),
    'RH_HM': ('%', 'Flagged_RH'),
    'Precip_HS': ('mm', 'Flagged_Precip'),
}
HALF_HOURLY_VARIABLES = {k.replace('_HM', '_HHM').replace('_HS', '_HHS'): v for k,v in VARIABLES.items()}
DIRECTIONS = {'ym': (-1, 0), 'yp': (1, 0), 'xm': (0, -1), 'xp': (0, 1)}


def cell_id(cell):
    return f'wn_grid_y{cell[0]}_x{cell[1]}'


def parse_cells(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
    cells = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r'wn_grid_y(0|[1-9][0-9]*)_x(0|[1-9][0-9]*)', line.strip())
        if not match:
            raise ValueError(f'Invalid cell ID: {line!r}')
        cells.append(tuple(map(int, match.groups())))
    if not cells or len(cells) > 100 or len(cells) != len(set(cells)):
        raise ValueError('Select 1..100 unique cells')
    return cells


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def observation_id(digest, variable, time, y, x):
    text = f'wegenernet-observation-v1|{digest}|{variable}|{time.isoformat()}|{y}|{x}'
    return hashlib.sha256(text.encode()).hexdigest()


def as_utc(value, interval_seconds=3600):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Explicit timezone required')
    value = value.astimezone(timezone.utc)
    if value.timestamp() % interval_seconds:
        raise ValueError('Cadence-aligned boundaries required')
    return value


def utc_date(raw):
    return datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second,
                    getattr(raw,'microsecond',0), tzinfo=timezone.utc)


def decoded_times(f):
    t = f['time']
    return [utc_date(v) for v in num2date(t[:], t.units, calendar=t.calendar,
                                        only_use_cftime_datetimes=False)]


class GridSource:
    """Preflight every file; load one hour of spatial fields at a time.

    Date range selects source intervals [start,end): their end labels are
    start+1h through end inclusive. No observations are invented for warm-up.
    """
    def __init__(self, paths, cells, start, end, warmup_hours=168, interval_seconds=3600):
        if interval_seconds not in (1800,3600):
            raise ValueError("Supported intervals: 1800 or 3600 seconds")
        self.interval_seconds = interval_seconds
        self.variables = HALF_HOURLY_VARIABLES if interval_seconds == 1800 else VARIABLES
        self.start, self.end = as_utc(start,interval_seconds), as_utc(end,interval_seconds)
        if self.end <= self.start or not 0 <= warmup_hours <= 8760:
            raise ValueError('Invalid interval or warm-up')
        if not cells or len(cells) != len(set(cells)) or len(cells) > 100:
            raise ValueError('Select 1..100 unique cells')
        self.cells = sorted(cells)
        self.files, labels = [], []
        grid = None
        paths = list(map(Path, paths))
        if not paths:
            raise ValueError('No source files')
        for path in paths:
            with Dataset(path) as f:
                if f['UTM33N'].epsg_code != 'EPSG:32633':
                    raise ValueError('Unsupported projection')
                xy = (tuple(map(float, f['Y'][:])), tuple(map(float, f['X'][:])))
                if grid is not None and grid != xy:
                    raise ValueError('Grid changed across files')
                grid = xy
                if any(np.any(np.diff(axis) <= 0) for axis in xy):
                    raise ValueError('Expected increasing UTM axes')
                ny, nx = len(xy[0]), len(xy[1])
                if any(not (0 <= y < ny and 0 <= x < nx) for y, x in cells):
                    raise ValueError('Cell outside actual grid')
                for name, (unit, flag) in self.variables.items():
                    if f[name].dimensions != ('time','Y','X') or f[name].units != unit:
                        raise ValueError(f'Unsupported variable schema: {name}')
                    if f[flag].dimensions != ('time','Y','X') or f[flag].units != '%':
                        raise ValueError(f'Unsupported flag schema: {flag}')
                times = decoded_times(f)
                if any(t.timestamp() % self.interval_seconds for t in times):
                    raise ValueError('Source timestamps must be hour-aligned UTC' if self.interval_seconds==3600 else 'Source timestamps must be half-hour-aligned UTC')
                if not times:
                    raise ValueError('Empty source file')
                b = f['time_bnds']
                bounds = num2date(b[:], b.units, calendar=f['time'].calendar,
                                  only_use_cftime_datetimes=False)
                for dt, (a, z) in zip(times, bounds):
                    if utc_date(z) != dt or (z-a).total_seconds() != self.interval_seconds:
                        raise ValueError('Expected end-labelled cadence intervals')
                if any((z-a).total_seconds() != self.interval_seconds for a,z in zip(times,times[1:])):
                    raise ValueError('Non-contiguous or duplicate source intervals')
                descriptor = dict(path=str(path), sha256=file_digest(path), first=times[0].isoformat(),
                                  last=times[-1].isoformat(), hours=len(times),
                                  metadata={n: {a: str(f[n].getncattr(a)) for a in f[n].ncattrs()}
                                            for n in f.variables})
                if self.interval_seconds == 1800:
                    descriptor["intervals"] = descriptor.pop("hours")
                self.files.append((times, descriptor))
                labels.extend(times)
        self.files.sort(key=lambda item:item[0][0])
        if len(set(labels)) != len(labels):
            raise ValueError('Overlapping source intervals')
        labels = sorted(t for t in labels if self.start-timedelta(hours=warmup_hours) < t <= self.end)
        expected = int((self.end-self.start).total_seconds()//self.interval_seconds)
        selected = [t for t in labels if self.start < t <= self.end]
        if len(selected) != expected:
            raise ValueError(f'Missing target source intervals: expected {expected}, found {len(selected)}')
        if any((z-a).total_seconds() != self.interval_seconds for a,z in zip(labels,labels[1:])):
            raise ValueError('Gap inside available warm-up/target data')
        self.available_warmup = len(labels)-expected
        self.warmup_hours = warmup_hours
        self.first = labels[0]
        self.neighbors = {cell_id(c): {d: cell_id((c[0]+dy,c[1]+dx))
            for d,(dy,dx) in DIRECTIONS.items()
            if 0 <= c[0]+dy < ny and 0 <= c[1]+dx < nx} for c in self.cells}
        self.input_cells = sorted(set(self.cells) | {(y+dy,x+dx) for y,x in self.cells
            for dy,dx in DIRECTIONS.values() if 0 <= y+dy < ny and 0 <= x+dx < nx})
        transform = Transformer.from_crs(32633,4326,always_xy=True)
        self.locations = {}
        for y,x in self.input_cells:
            lon,lat = transform.transform(xy[1][x],xy[0][y])
            self.locations[cell_id((y,x))] = Location(longitude=float(lon),latitude=float(lat))
        self.grid = dict(shape=[ny,nx], epsg=32633,
                         x_spacing_m=sorted(set(np.diff(xy[1]))), y_spacing_m=sorted(set(np.diff(xy[0]))))

    def hours(self):
        for times, descriptor in self.files:
            with Dataset(descriptor['path']) as f:
                for i, dt in enumerate(times):
                    if not self.first <= dt <= self.end:
                        continue
                    # A few MB per hour, independent of number of months supplied.
                    arrays = {n:f[n][i,:,:] for n in (*self.variables, 'Flagged_T','Flagged_RH','Flagged_Precip')}
                    samples, evidence = [], []
                    for y,x in self.input_cells:
                        sid = cell_id((y,x))
                        for name,(unit,flag) in self.variables.items():
                            oid = observation_id(descriptor['sha256'], name, dt, y, x)
                            sample = Measurement(sensor_id=sid, observed_at=dt, quantity=name,
                                value=number(arrays[name][y,x]),unit=unit,location=self.locations[sid],
                                source_id='wegenernet_v8',source_record_id=oid)
                            samples.append(sample)
                            evidence.append(dict(observation_id=oid,source_id=sample.source_id,
                                file_sha256=descriptor['sha256'],variable=name,time_index=i,y=y,x=x,
                                timestamp=dt.isoformat(),sensor_id=sid,raw_value=sample.value,unit=unit,
                                flag_field=flag,flag_percent=number(arrays[flag][y,x])))
                    yield dt,samples,evidence
