# Copyright 2022 The jax3d Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Dataclass array."""

from __future__ import annotations

import dataclasses
import typing
from typing import Any, Callable, Generic, Iterable, Iterator, Optional, Tuple, Type, TypeVar, Union

from etils import edc
from etils import enp
from etils import epy
from etils.array_types import Array
from jax3d.visu3d import np_utils
from jax3d.visu3d import py_utils
from jax3d.visu3d.typing import DcOrArray, DcOrArrayT, DTypeArg, Shape  # pylint: disable=g-multiple-import
import numpy as np
from typing_extensions import Literal

if typing.TYPE_CHECKING:
  from jax3d.visu3d import transformation

lazy = enp.lazy

# TODO(pytype): Should use `v3d.typing.DcT` but bound does not work across
# modules.
_DcT = TypeVar('_DcT', bound='DataclassArray')

# Any valid numpy indices slice ([x], [x:y], [:,...], ...)
_IndiceItem = Union[type(Ellipsis), None, int, slice, Any]
_Indices = Tuple[_IndiceItem]  # Normalized slicing
_IndicesArg = Union[_IndiceItem, _Indices]

_METADATA_KEY = 'v3d_field'


class DataclassArray:
  """Dataclass which behaves like an array.

  Usage:

  ```python
  @dataclasses.dataclass
  class Square(DataclassArray):
    pos: Array['*shape 2'] = array_field(shape=(2,))
    scale: Array['*shape'] = array_field(shape=())

  # Create 2 square batched
  p = Square(pos=[[x0, y0], [x1, y1], [x2, y2]], scale=[scale0, scale1, scale2])
  p.shape == (3,)
  p.pos.shape == (3, 2)
  p[0] == Square(pos=[x0, y0], scale=scale0)

  p = p.reshape((3, 1))  # Reshape the inner-shape
  p.shape == (3, 1)
  p.pos.shape == (3, 1, 2)
  ```

  """
  _shape: Shape
  _xnp: enp.NpModule

  def __init_subclass__(cls, **kwargs):
    super().__init_subclass__(**kwargs)
    # TODO(epot): Could have smart __repr__ which display types if array have
    # too many values.
    edc.dataclass_utils.add_repr(cls)
    cls._v3d_tree_map_registered = False

  def __post_init__(self) -> None:
    """Validate and normalize inputs."""
    cls = type(self)

    # Make sure the dataclass was registered and frozen
    if not dataclasses.is_dataclass(cls) or not cls.__dataclass_params__.frozen:  # pytype: disable=attribute-error
      raise ValueError(
          '`v3d.DataclassArray` need to be @dataclasses.dataclass(frozen=True)')

    # Register the tree_map here instead of `__init_subclass__` as `jax` may
    # not have been registered yet during import
    if enp.lazy.has_jax and not cls._v3d_tree_map_registered:  # pylint: disable=protected-access
      enp.lazy.jax.tree_util.register_pytree_node_class(cls)
      cls._v3d_tree_map_registered = True  # pylint: disable=protected-access

    # Note: Calling the `_all_array_fields` property during `__init__` will
    # normalize the arrays (`list` -> `np.ndarray`). This is done in the
    # `_ArrayField` contructor
    if not self._all_array_fields:
      raise ValueError(
          f'{self.__class__.__qualname__} should have at least one '
          '`v3d.array_field`')

    # Validate the array type is consistent (all np or all jnp but not both)
    xnps = py_utils.groupby(
        self._array_fields,
        key=lambda f: f.xnp,
        value=lambda f: f.name,
    )
    if len(xnps) > 1:
      xnps = {k.__name__: v for k, v in xnps.items()}
      raise ValueError(f'Conflicting numpy types: {xnps}')

    # Validate the batch shape is consistent
    shapes = py_utils.groupby(
        self._array_fields,
        key=lambda f: f.host_shape,
        value=lambda f: f.name,
    )
    if len(shapes) > 1:
      raise ValueError(f'Conflicting batch shapes: {shapes}')

    if not xnps:  # No values
      # Inside `jax.tree_utils`, tree-def can be created with `None` values.
      assert not shapes
      xnps = (np,)
      shapes = (None,)

    # TODO(epot): Support broadcasting

    # Cache results
    (xnp,) = xnps
    (shape,) = shapes
    # Should the state be stored in a separate object to avoid collisions ?
    assert shape is None or isinstance(shape, tuple), shape
    self._setattr('_shape', shape)
    self._setattr('_xnp', xnp)

  # ====== Array functions ======

  @property
  def shape(self) -> Shape:
    """Returns the batch shape common to all fields."""
    return self._shape

  @property
  def size(self) -> int:
    """Returns the batch shape common to all fields."""
    return np_utils.size_of(self._shape)

  def reshape(self: _DcT, shape: Union[tuple[int, ...], str]) -> _DcT:
    """Reshape the batch shape according to the pattern."""
    if isinstance(shape, str):
      # TODO(epot): Have an einops.rearange version which only look at the
      # first `self.shape` dims.
      # einops.rearrange(x,)
      raise NotImplementedError

    def _reshape(f: _ArrayField):
      return f.value.reshape(shape + f.inner_shape)

    return self._map_field(_reshape, nest_fn=_reshape)

  def flatten(self: _DcT) -> _DcT:
    """Flatten the batch shape."""
    return self.reshape((-1,))

  def broadcast_to(self: _DcT, shape: Shape) -> _DcT:
    """Broadcast the batch shape."""
    return self._map_field(
        lambda f: self.xnp.broadcast_to(f.value, shape + f.inner_shape),
        nest_fn=lambda f: f.value.broadcast_to(shape + f.inner_shape),
    )

  def __getitem__(self: _DcT, indices: _IndicesArg) -> _DcT:
    """Slice indexing."""
    indices = np.index_exp[indices]  # Normalize indices
    # Replace `...` by explicit shape
    indices = _to_absolute_indices(indices, shape=self.shape)
    return self._map_field(
        lambda f: f.value[indices],
        nest_fn=lambda f: f.value[indices],
    )

  # _DcT[n *d] -> Iterator[_DcT[*d]]
  def __iter__(self: _DcT) -> Iterator[_DcT]:
    """Iterate over the outermost dimension."""
    if not self.shape:
      raise TypeError(f'iteration over 0-d array: {self!r}')

    # Similar to `etree.unzip(self)` (but work with any backend)
    field_names = [f.name for f in self._array_fields]
    field_values = [f.value for f in self._array_fields]
    for vals in zip(*field_values):
      yield self.replace(**dict(zip(field_names, vals)))

  def __len__(self) -> int:
    """Length of the first array dimension."""
    if not self.shape:
      raise TypeError(
          f'len() of unsized {self.__class__.__name__} (shape={self.shape})')
    return self.shape[0]

  def __bool__(self) -> Literal[True]:
    """`v3d.DataclassArray` always evaluate to `True`.

    Like all python objects (including dataclasses), `v3d.DataclassArray` always
    evaluate to `True`. So:
    `Ray(pos=None)`, `Ray(pos=0)` all evaluate to `True`.

    This allow construct like:

    ```python
    def fn(ray: Optional[v3d.Ray] = None):
      if ray:
        ...
    ```

    Or:

    ```python
    def fn(ray: Optional[v3d.Ray] = None):
      ray = ray or default_ray
    ```

    Only in the very rare case of empty-tensor (`shape=(0, ...)`)

    ```python
    assert ray is not None
    assert len(ray) == 0
    bool(ray)  # TypeError: Truth value is ambigous
    ```

    Returns:
      True

    Raises:
      ValueError: If `len(self) == 0` to avoid ambiguity.
    """
    if self.shape and not len(self):  # pylint: disable=g-explicit-length-test
      raise ValueError(
          f'The truth value of {self.__class__.__name__} when `len(x) == 0` '
          'is ambigous. Use `len(x)` or `x is not None`.')
    return True

  def map_field(
      self: _DcT,
      fn: Callable[[Array['*din']], Array['*dout']],
  ) -> _DcT:
    """Apply a transformation on all arrays from the fields."""
    return self._map_field(
        lambda f: fn(f.value),
        nest_fn=lambda f: f.value.map_field(fn),
    )

  # ====== Dataclass/Conversion utils ======

  replace = edc.dataclass_utils.replace

  def as_np(self: _DcT) -> _DcT:
    """Returns the instance as containing `np.ndarray`."""
    return self.as_xnp(enp.lazy.np)

  def as_jax(self: _DcT) -> _DcT:
    """Returns the instance as containing `jnp.ndarray`."""
    return self.as_xnp(enp.lazy.jnp)

  def as_tf(self: _DcT) -> _DcT:
    """Returns the instance as containing `tf.Tensor`."""
    return self.as_xnp(enp.lazy.tnp)

  def as_xnp(self: _DcT, xnp: enp.NpModule) -> _DcT:
    """Returns the instance as containing `xnp.ndarray`."""
    return self.map_field(xnp.asarray)

  # ====== Internal ======

  @property
  def xnp(self) -> enp.NpModule:
    """Returns the numpy module of the class (np, jnp, tnp)."""
    return self._xnp

  @epy.cached_property
  def _all_array_fields(self) -> dict[str, _ArrayField]:
    """All array fields, including `None` values."""
    # Validate and normalize array fields (e.g. list -> np.array,...)
    return {  # pylint: disable=g-complex-comprehension
        f.name: _ArrayField(  # pylint: disable=g-complex-comprehension
            name=f.name,
            host=self,
            **f.metadata[_METADATA_KEY].to_dict(),
        ) for f in dataclasses.fields(self) if _METADATA_KEY in f.metadata
    }

  @epy.cached_property
  def _array_fields(self) -> list[_ArrayField]:
    """All active array fields (non-None)."""
    # Filter `None` values
    return [
        f for f in self._all_array_fields.values() if not f.is_value_missing
    ]

  def apply_transform(
      self: _DcT,
      tr: transformation.Transform,
  ) -> _DcT:
    """Transform protocol.

    Applied the transformation on it-self. Called during:

    ```python
    my_obj = tr @ my_obj  # Call `my_obj.apply_transform(tr)`
    ```

    Inside this function, `tr.shape == ()`. Vectorization is auto-supported.

    Args:
      tr: Transformation to apply (will always have `tr.shape == ()`)
    """
    raise NotImplementedError(
        f'{self.__class__.__qualname__} does not support `v3d.Transform`.')

  # TODO(epot): Should we have a non-batched version where the transformation
  # is applied on each leaf (with some vectorization) ?
  # Like: .map_leaf(Callable[[_DcT], _DcT])
  # Would be trickier to support np/TF.
  def _map_field(
      self: _DcT,
      fn: Callable[[_ArrayField], Array['*dout']],
      nest_fn: Optional[Callable[[_ArrayField[_DcT]], _DcT]] = None,
  ) -> _DcT:
    """Apply a transformation on all array fields structure.

    Args:
      fn: Function applied on the leaf (`xnp.ndarray`)
      nest_fn: Function applied on the `v3d.DataclassArray` (to recurse)

    Returns:
      The transformed dataclass array.
    """

    def _apply_field_dn(f: _ArrayField):
      if f.is_dataclass:  # Recurse on dataclasses
        if nest_fn is None:
          raise NotImplementedError(
              'Function does not support nested dataclasses')
        return nest_fn(f)  # pylint: disable=protected-access
      else:
        return fn(f)

    new_values = {f.name: _apply_field_dn(f) for f in self._array_fields}
    return self.replace(**new_values)

  def tree_flatten(self) -> tuple[list[DcOrArray], _TreeMetadata]:
    """`jax.tree_utils` support."""
    # We flatten all values (and not just the non-None ones)
    array_field_values = [f.value for f in self._all_array_fields.values()]
    metadata = _TreeMetadata(
        array_field_names=list(self._all_array_fields.keys()),
        non_array_field_kwargs={
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if f.name not in self._all_array_fields
        },
    )
    return (array_field_values, metadata)

  @classmethod
  def tree_unflatten(
      cls: Type[_DcT],
      metadata: _TreeMetadata,
      array_field_values: list[DcOrArray],
  ) -> _DcT:
    """`jax.tree_utils` support."""
    array_field_kwargs = dict(
        zip(metadata.array_field_names, array_field_values))
    return cls(**array_field_kwargs, **metadata.non_array_field_kwargs)

  def _setattr(self, name: str, value: Any) -> None:
    """Like setattr, but support `frozen` dataclasses."""
    object.__setattr__(self, name, value)

  def assert_same_xnp(self, x: Union[Array[...], DataclassArray]) -> None:
    """Assert the given array is of the same type as the current object."""
    xnp = np_utils.get_xnp(x)
    if xnp is not self.xnp:
      raise ValueError(
          f'{self.__class__.__name__} is {self.xnp.__name__} but got input '
          f'{xnp.__name__}. Please cast input first.')


def stack(
    arrays: Iterable[_DcT],  # list[_DcT['*shape']]
    *,
    axis: int = 0,
) -> _DcT:  # _DcT['len(arrays) *shape']:
  """Stack dataclasses together."""
  arrays = list(arrays)
  first_arr = arrays[0]

  # This might have some edge cases if user try to stack subclasses
  types = py_utils.groupby(
      arrays,
      key=type,
      value=lambda x: type(x).__name__,
  )
  if False in types:
    raise TypeError(
        f'v3.stack got conflicting types as input: {list(types.values())}')

  xnp = first_arr.xnp
  if axis != 0:
    # If axis < 0, we should normalize the axis such as the last axis is
    # before the inner shape
    # axis = self._to_absolute_axis(axis)
    raise NotImplementedError('Please open an issue.')

  # Iterating over only the fields of the `first_arr` will skip optional fields
  # if those are not set in `first_arr`, even if they are present in others.
  # But is consistent with `jax.tree_map`:
  # jax.tree_map(lambda x, y: x+y, (None, 10), (1, 2)) == (None, 12)
  merged_arr = first_arr._map_field(  # pylint: disable=protected-access
      lambda f: xnp.stack([getattr(arr, f.name) for arr in arrays], axis=axis),
      nest_fn=lambda f: stack([getattr(arr, f.name) for arr in arrays]),
  )
  return merged_arr


def _count_not_none(indices: _Indices) -> int:
  """Count the number of non-None and non-ellipsis elements."""
  return len([k for k in indices if k is not np.newaxis and k is not Ellipsis])


def _count_ellipsis(elems: _Indices) -> int:
  """Returns the number of `...` in the indices."""
  # Cannot use `elems.count(Ellipsis)` because `np.array() == Ellipsis` fail
  return len([elem for elem in elems if elem is Ellipsis])


def _to_absolute_indices(indices: _Indices, *, shape: Shape) -> _Indices:
  """Normalize the indices to replace `...`, by `:, :, :`."""
  assert isinstance(indices, tuple)
  ellipsis_count = _count_ellipsis(indices)
  if ellipsis_count > 1:
    raise IndexError("an index can only have a single ellipsis ('...')")
  valid_count = _count_not_none(indices)
  if valid_count > len(shape):
    raise IndexError(f'too many indices for array. Batch shape is {shape}, but '
                     f'rank-{valid_count} was provided.')
  if not ellipsis_count:
    return indices
  ellipsis_index = indices.index(Ellipsis)
  start_elems = indices[:ellipsis_index]
  end_elems = indices[ellipsis_index + 1:]
  ellipsis_replacement = [slice(None)] * (len(shape) - valid_count)
  return (*start_elems, *ellipsis_replacement, *end_elems)


@dataclasses.dataclass(frozen=True)
class _TreeMetadata:
  """Metadata forwarded in ``."""
  array_field_names: list[str]
  non_array_field_kwargs: dict[str, Any]


def array_field(
    shape: Shape,
    dtype: DTypeArg = float,
    **field_kwargs,
) -> dataclasses.Field:
  """Dataclass array field.

  See `v3d.DataclassArray` for example.

  Args:
    shape: Inner shape of the field
    dtype: Type of the field
    **field_kwargs: Args forwarded to `dataclasses.field`

  Returns:
    The dataclass field.
  """
  # TODO(epot): Validate shape, dtype
  v3d_field = _ArrayFieldMetadata(
      inner_shape=shape,
      dtype=dtype,
  )
  return dataclasses.field(**field_kwargs, metadata={_METADATA_KEY: v3d_field})


@edc.dataclass
@dataclasses.dataclass
class _ArrayFieldMetadata:
  """Metadata of the array field (shared across all instances).

  Attributes:
    inner_shape: Inner shape
    dtype: Type of the array. Can be `int`, `float`, `np.dtype` or
      `v3d.DataclassArray` for nested arrays.
  """
  inner_shape: Shape
  dtype: DTypeArg

  def __post_init__(self):
    """Normalizing/validating the shape/dtype."""
    # Validate shape
    self.inner_shape = tuple(self.inner_shape)
    if None in self.inner_shape:
      raise ValueError(f'Shape should be defined. Got: {self.inner_shape}')

    # Validate dtype
    if not self.is_dataclass:
      dtype = self.dtype
      if dtype is int:
        dtype = np.int32
      elif dtype is float:
        dtype = np.float32

      dtype = np.dtype(dtype)
      if dtype.kind == 'O':
        raise ValueError(f'Array field dtype={self.dtype} not supported.')
      self.dtype = dtype

  def to_dict(self) -> dict[str, Any]:
    """Returns the dict[field_name, field_value]."""
    return {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}

  @property
  def is_dataclass(self) -> bool:
    """Returns `True` if the field is a dataclass."""
    # Need to check `type` first as `issubclass` fails for `np.dtype('int32')`
    dtype = self.dtype
    return isinstance(dtype, type) and issubclass(dtype, DataclassArray)


@edc.dataclass
@dataclasses.dataclass
class _ArrayField(_ArrayFieldMetadata, Generic[DcOrArrayT]):
  """Array field of a specific dataclass instance.

  Attributes:
    name: Instance of the attribute
    host: Dataclass instance who this field is attached too
    xnp: Numpy module
  """
  name: str
  host: DataclassArray
  xnp: enp.NpModule = dataclasses.field(init=False)

  def __post_init__(self):
    if self.is_value_missing:  # No validation when there is no value
      return
    if self.is_dataclass:
      self._init_dataclass()
    else:
      self._init_array()

    # Common assertions to all fields types
    if self.host_shape + self.inner_shape != self.value.shape:
      raise ValueError(f'Expected last dimensions to be {self.inner_shape} for '
                       f'field {self.name!r} with shape {self.value.shape}')

  def _init_array(self) -> None:
    """Initialize when the field is an array."""
    if isinstance(self.value, DataclassArray):
      raise TypeError(
          f'{self.name} should be {self.dtype}. Got: {type(self.value)}')
    # Convert and normalize the array
    self.xnp = lazy.get_xnp(self.value, strict=False)
    value = self.xnp.asarray(self.value, dtype=self.dtype)
    self.host._setattr(self.name, value)  # pylint: disable=protected-access

  def _init_dataclass(self) -> None:
    """Initialize when the field is a nested dataclass array."""
    if not isinstance(self.value, self.dtype):
      raise TypeError(
          f'{self.name} should be {self.dtype}. Got: {type(self.value)}')
    self.xnp = self.value.xnp

  @property
  def value(self) -> DcOrArrayT:
    """Access the `host.<field-name>`."""
    return getattr(self.host, self.name)

  @property
  def is_value_missing(self) -> bool:
    """Returns `True` if the value wasn't set."""
    if self.value is None:
      return True
    elif type(self.value) is object:  # pylint: disable=unidiomatic-typecheck
      # Checking for `object` is a hack required for `@jax.vmap` compatibility:
      # In `jax/_src/api_util.py` for `flatten_axes`, jax set all values to a
      # dummy sentinel `object()` value.
      return True
    elif (
        isinstance(self.value, DataclassArray) and
        not self.value._array_fields  # pylint: disable=protected-access
    ):
      # Nested dataclass case (if all attributes are `None`, so no active
      # array fields)
      return True
    return False

  @property
  def host_shape(self) -> Shape:
    """Host shape (batch shape shared by all fields)."""
    if not self.inner_shape:
      shape = self.value.shape
    else:
      shape = self.value.shape[:-len(self.inner_shape)]
    # TODO(b/198633198): We need to convert to tuple because TF evaluate
    # empty shapes to True `bool(shape) == True` when `shape=()`
    return tuple(shape)
