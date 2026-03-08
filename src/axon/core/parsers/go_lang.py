"""Go language parser using tree-sitter.

Extracts functions, methods (with receivers), structs, interfaces, type aliases,
enums (iota const blocks), imports, call expressions, type annotation references,
and inheritance/implementation relationships from Go source code.

Supports Go 1.18+ generics, embedded struct fields (treated as extends),
and interface embedding (treated as implements).
"""

from __future__ import annotations

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from axon.core.parsers.base import (
    CallInfo,
    ImportInfo,
    LanguageParser,
    ParseResult,
    SymbolInfo,
    TypeRef,
)

GO_LANGUAGE = Language(tsgo.language())

# Built-in types that should NOT create USES_TYPE edges
_BUILTIN_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "int",
        "int8",
        "int16",
        "int32",
        "int64",
        "uint",
        "uint8",
        "uint16",
        "uint32",
        "uint64",
        "uintptr",
        "float32",
        "float64",
        "complex64",
        "complex128",
        "bool",
        "byte",
        "rune",
        "error",
        "any",
        "comparable",
        "interface",
    }
)

# Standard library packages — we skip type refs to these to reduce noise
_STDLIB_PACKAGES: frozenset[str] = frozenset(
    {
        "fmt",
        "os",
        "io",
        "log",
        "time",
        "sync",
        "context",
        "strings",
        "strconv",
        "errors",
        "math",
        "net",
        "http",
        "json",
        "bytes",
        "bufio",
        "sort",
        "path",
        "filepath",
        "regexp",
        "runtime",
        "reflect",
        "testing",
        "crypto",
        "encoding",
        "database",
        "sql",
        "atomic",
    }
)


class GoParser(LanguageParser):
    """Parses Go source code using tree-sitter.

    Handles all major Go constructs:
    - Package-level functions and methods (with receiver type extraction)
    - Structs (as classes) with embedded field detection for heritage
    - Interfaces with method sets and embedding
    - Type aliases and named types
    - Const blocks with iota (as enums)
    - Import declarations (single and grouped)
    - Function/method calls with receiver tracking
    - Type references from parameters, return types, and variable declarations
    """

    def __init__(self) -> None:
        self._parser = Parser(GO_LANGUAGE)

    def parse(self, content: str, file_path: str) -> ParseResult:
        """Parse Go source and return structured information."""
        tree = self._parser.parse(content.encode("utf-8"))
        result = ParseResult()
        self._walk(tree.root_node, content, result)
        return result

    # ------------------------------------------------------------------
    # AST walking
    # ------------------------------------------------------------------

    def _walk(self, node: Node, content: str, result: ParseResult) -> None:
        """Recursively walk the AST dispatching on node type."""
        for child in node.children:
            ntype = child.type
            if ntype == "function_declaration":
                self._extract_function(child, content, result)
            elif ntype == "method_declaration":
                self._extract_method(child, content, result)
            elif ntype == "type_declaration":
                self._extract_type_declaration(child, content, result)
            elif ntype == "import_declaration":
                self._extract_imports(child, result)
            elif ntype == "const_declaration":
                self._extract_const_block(child, content, result)
            elif ntype == "var_declaration":
                self._extract_var_types(child, result)
            # Recurse into blocks we haven't specifically handled
            elif ntype in ("source_file",):
                self._walk(child, content, result)

        # Extract all calls from the full tree
        if node.type == "source_file":
            self._extract_calls_recursive(node, result)

    # ------------------------------------------------------------------
    # Function extraction
    # ------------------------------------------------------------------

    def _extract_function(
        self, node: Node, content: str, result: ParseResult
    ) -> None:
        """Extract a top-level function declaration."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return

        name = name_node.text.decode("utf-8")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        node_content = content[node.start_byte : node.end_byte]
        signature = self._build_function_signature(node, content)

        result.symbols.append(
            SymbolInfo(
                name=name,
                kind="function",
                start_line=start_line,
                end_line=end_line,
                content=node_content,
                signature=signature,
            )
        )

        # Extract parameter and return type references
        self._extract_param_types(node, result)
        self._extract_return_types(node, result)

    def _extract_method(
        self, node: Node, content: str, result: ParseResult
    ) -> None:
        """Extract a method declaration (function with receiver)."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return

        name = name_node.text.decode("utf-8")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        node_content = content[node.start_byte : node.end_byte]

        # Extract receiver type name
        receiver_type = self._extract_receiver_type(node)
        signature = self._build_method_signature(node, content, receiver_type)

        result.symbols.append(
            SymbolInfo(
                name=name,
                kind="method",
                start_line=start_line,
                end_line=end_line,
                content=node_content,
                signature=signature,
                class_name=receiver_type,
            )
        )

        # Extract parameter and return type references
        self._extract_param_types(node, result)
        self._extract_return_types(node, result)

    def _extract_receiver_type(self, method_node: Node) -> str:
        """Extract the receiver type name from a method declaration.

        Handles both value and pointer receivers:
        - ``func (s UserService) Method()`` -> ``"UserService"``
        - ``func (s *UserService) Method()`` -> ``"UserService"``
        """
        receiver = method_node.child_by_field_name("receiver")
        if receiver is None:
            return ""

        for child in receiver.children:
            if child.type == "parameter_declaration":
                # Find the type identifier, stripping pointer if present
                for sub in child.children:
                    if sub.type == "type_identifier":
                        return sub.text.decode("utf-8")
                    if sub.type == "pointer_type":
                        for ptr_child in sub.children:
                            if ptr_child.type == "type_identifier":
                                return ptr_child.text.decode("utf-8")
        return ""

    # ------------------------------------------------------------------
    # Type declarations (struct, interface, type alias, named type)
    # ------------------------------------------------------------------

    def _extract_type_declaration(
        self, node: Node, content: str, result: ParseResult
    ) -> None:
        """Extract type declarations: struct, interface, type alias, named type."""
        for child in node.children:
            if child.type == "type_spec":
                self._extract_type_spec(child, content, result)

    def _extract_type_spec(
        self, node: Node, content: str, result: ParseResult
    ) -> None:
        """Extract a single type_spec node."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return

        type_name = name_node.text.decode("utf-8")
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        # Find the actual type definition (struct_type, interface_type, or alias)
        type_node = node.child_by_field_name("type")
        if type_node is None:
            # Check children for the type definition
            for child in node.children:
                if child.type in (
                    "struct_type",
                    "interface_type",
                    "type_identifier",
                    "pointer_type",
                    "slice_type",
                    "map_type",
                    "function_type",
                    "channel_type",
                    "qualified_type",
                ):
                    type_node = child
                    break

        if type_node is None:
            return

        # Get the full content from the parent type_declaration if available
        decl_node = node.parent if node.parent and node.parent.type == "type_declaration" else node
        node_content = content[decl_node.start_byte : decl_node.end_byte]

        if type_node.type == "struct_type":
            # Struct -> treated as a class
            result.symbols.append(
                SymbolInfo(
                    name=type_name,
                    kind="class",
                    start_line=start_line,
                    end_line=end_line,
                    content=node_content,
                    signature=f"type {type_name} struct",
                )
            )
            # Extract embedded fields as heritage (extends)
            self._extract_struct_heritage(type_name, type_node, result)

        elif type_node.type == "interface_type":
            # Interface
            result.symbols.append(
                SymbolInfo(
                    name=type_name,
                    kind="interface",
                    start_line=start_line,
                    end_line=end_line,
                    content=node_content,
                    signature=f"type {type_name} interface",
                )
            )
            # Extract embedded interfaces as heritage (implements)
            self._extract_interface_heritage(type_name, type_node, result)

        elif type_node.type == "type_identifier":
            # Named type alias: type Status int, type Handler func(...)
            underlying = type_node.text.decode("utf-8")
            result.symbols.append(
                SymbolInfo(
                    name=type_name,
                    kind="type_alias",
                    start_line=start_line,
                    end_line=end_line,
                    content=node_content,
                    signature=f"type {type_name} {underlying}",
                )
            )
        else:
            # Other type definitions (slice, map, func, channel, etc.)
            result.symbols.append(
                SymbolInfo(
                    name=type_name,
                    kind="type_alias",
                    start_line=start_line,
                    end_line=end_line,
                    content=node_content,
                    signature=f"type {type_name} {type_node.type}",
                )
            )

    def _extract_struct_heritage(
        self, struct_name: str, struct_node: Node, result: ParseResult
    ) -> None:
        """Extract embedded fields from a struct as 'extends' heritage.

        In Go, embedded structs act like inheritance:
        ``type Server struct { BaseServer; ... }`` means Server extends BaseServer.
        """
        field_list = struct_node.child_by_field_name("field_declaration_list")
        if field_list is None:
            # Try iterating children directly
            for child in struct_node.children:
                if child.type == "field_declaration_list":
                    field_list = child
                    break
        if field_list is None:
            return

        for field in field_list.children:
            if field.type == "field_declaration":
                # Embedded field has no name, only a type
                children = [c for c in field.children if c.is_named]
                if len(children) == 1:
                    # Single named child = embedded field
                    embedded = children[0]
                    embedded_name = self._extract_type_name_from_node(embedded)
                    if embedded_name and embedded_name not in _BUILTIN_TYPES:
                        result.heritage.append(
                            (struct_name, "extends", embedded_name)
                        )

    def _extract_interface_heritage(
        self, iface_name: str, iface_node: Node, result: ParseResult
    ) -> None:
        """Extract embedded interfaces as 'implements' heritage.

        ``type ReadCloser interface { Reader; Closer }`` means
        ReadCloser embeds Reader and Closer.
        """
        for child in iface_node.children:
            if child.type == "type_identifier":
                parent_name = child.text.decode("utf-8")
                if parent_name not in _BUILTIN_TYPES:
                    result.heritage.append(
                        (iface_name, "extends", parent_name)
                    )
            elif child.type == "qualified_type":
                parent_name = self._extract_type_name_from_node(child)
                if parent_name and parent_name not in _BUILTIN_TYPES:
                    result.heritage.append(
                        (iface_name, "extends", parent_name)
                    )
            # method_elem nodes are interface method signatures — skip for heritage

    # ------------------------------------------------------------------
    # Const/iota blocks (Go enums)
    # ------------------------------------------------------------------

    def _extract_const_block(
        self, node: Node, content: str, result: ParseResult
    ) -> None:
        """Extract const blocks with iota as enum-like definitions.

        A const block with iota is Go's idiomatic enum pattern:
        ``const ( StatusPending Status = iota; StatusRunning; ... )``
        """
        # Check if any const_spec uses iota
        has_iota = False
        type_name = ""
        specs: list[Node] = []

        for child in node.children:
            if child.type == "const_spec":
                specs.append(child)
                # Check for iota in expression list
                for sub in child.children:
                    if sub.type == "expression_list":
                        if "iota" in sub.text.decode("utf-8"):
                            has_iota = True
                    if sub.type == "type_identifier":
                        type_name = sub.text.decode("utf-8")

        if not has_iota or not type_name:
            return

        # Collect all enum values
        enum_values: list[str] = []
        for spec in specs:
            for sub in spec.children:
                if sub.type == "identifier":
                    enum_values.append(sub.text.decode("utf-8"))
                    break

        if enum_values:
            start_line = node.start_point[0] + 1
            end_line = node.end_point[0] + 1
            node_content = content[node.start_byte : node.end_byte]

            result.symbols.append(
                SymbolInfo(
                    name=type_name,
                    kind="enum",
                    start_line=start_line,
                    end_line=end_line,
                    content=node_content,
                    signature=f"const ({', '.join(enum_values)})",
                )
            )

    # ------------------------------------------------------------------
    # Import extraction
    # ------------------------------------------------------------------

    def _extract_imports(self, node: Node, result: ParseResult) -> None:
        """Extract import declarations (single and grouped).

        Handles:
        - ``import "fmt"``
        - ``import ( "fmt"; "net/http"; alias "pkg/name" )``
        """
        for child in node.children:
            if child.type == "import_spec":
                self._extract_single_import(child, result)
            elif child.type == "import_spec_list":
                for spec in child.children:
                    if spec.type == "import_spec":
                        self._extract_single_import(spec, result)

    def _extract_single_import(self, node: Node, result: ParseResult) -> None:
        """Extract a single import spec.

        ``import alias "github.com/org/pkg/subpkg"`` produces:
        - module = "github.com/org/pkg/subpkg"
        - names = ["subpkg"]
        - alias = "alias" (if present)
        """
        path_node = None
        alias_node = None

        for child in node.children:
            if child.type == "interpreted_string_literal":
                path_node = child
            elif child.type == "package_identifier":
                alias_node = child
            elif child.type == "dot":
                # dot import: import . "pkg"
                alias_node = child
            elif child.type == "blank_identifier":
                # blank import: import _ "pkg"
                alias_node = child

        if path_node is None:
            return

        # Strip quotes from import path
        import_path = path_node.text.decode("utf-8").strip('"')

        # The local name is the last segment of the path
        parts = import_path.split("/")
        local_name = parts[-1] if parts else import_path

        # Handle version suffixes like "v2" — use the package before it
        if local_name.startswith("v") and local_name[1:].isdigit() and len(parts) > 1:
            local_name = parts[-2]

        alias = ""
        if alias_node is not None:
            alias_text = alias_node.text.decode("utf-8")
            if alias_text not in (".", "_"):
                alias = alias_text

        result.imports.append(
            ImportInfo(
                module=import_path,
                names=[local_name],
                is_relative=not ("/" in import_path or "." in import_path),
                alias=alias,
            )
        )

    # ------------------------------------------------------------------
    # Call extraction
    # ------------------------------------------------------------------

    def _extract_calls_recursive(self, node: Node, result: ParseResult) -> None:
        """Recursively extract all call expressions from the AST."""
        if node.type == "call_expression":
            self._extract_call(node, result)

        for child in node.children:
            self._extract_calls_recursive(child, result)

    def _extract_call(self, call_node: Node, result: ParseResult) -> None:
        """Extract a single call expression.

        Handles:
        - ``functionName(args)`` -> name="functionName"
        - ``pkg.FunctionName(args)`` -> name="FunctionName", receiver="pkg"
        - ``obj.Method(args)`` -> name="Method", receiver="obj"
        - ``obj.field.Method(args)`` -> name="Method", receiver="obj"
        """
        func_node = call_node.child_by_field_name("function")
        if func_node is None:
            return

        line = call_node.start_point[0] + 1
        arguments = self._extract_identifier_arguments(call_node)

        if func_node.type == "identifier":
            # Simple function call: myFunc(args)
            name = func_node.text.decode("utf-8")
            result.calls.append(
                CallInfo(name=name, line=line, arguments=arguments)
            )

        elif func_node.type == "selector_expression":
            # Method or package-qualified call: obj.Method(args) or pkg.Func(args)
            name, receiver = self._extract_selector_call(func_node)
            if name:
                result.calls.append(
                    CallInfo(
                        name=name,
                        line=line,
                        receiver=receiver,
                        arguments=arguments,
                    )
                )

    def _extract_selector_call(self, selector_node: Node) -> tuple[str, str]:
        """Extract (method_name, receiver) from a selector_expression.

        ``obj.Method`` -> ("Method", "obj")
        ``pkg.subpkg.Func`` -> ("Func", "pkg")
        ``obj.field.Method`` -> ("Method", "obj")
        """
        field_node = selector_node.child_by_field_name("field")
        operand_node = selector_node.child_by_field_name("operand")

        method_name = ""
        if field_node is not None:
            method_name = field_node.text.decode("utf-8")

        receiver = ""
        if operand_node is not None:
            receiver = self._root_identifier(operand_node)

        return method_name, receiver

    @staticmethod
    def _extract_identifier_arguments(call_node: Node) -> list[str]:
        """Extract bare identifier arguments from a call (likely callbacks/handlers).

        Only extracts plain identifiers, not literals or complex expressions.
        """
        args_node = call_node.child_by_field_name("arguments")
        if args_node is None:
            return []

        identifiers: list[str] = []
        for child in args_node.children:
            if child.type == "identifier":
                identifiers.append(child.text.decode("utf-8"))
        return identifiers

    # ------------------------------------------------------------------
    # Type reference extraction
    # ------------------------------------------------------------------

    def _extract_param_types(self, func_node: Node, result: ParseResult) -> None:
        """Extract type references from function/method parameters."""
        params = func_node.child_by_field_name("parameters")
        if params is None:
            return

        for child in params.children:
            if child.type == "parameter_declaration":
                self._extract_param_declaration_types(child, result, "param")

    def _extract_return_types(self, func_node: Node, result: ParseResult) -> None:
        """Extract type references from return types.

        Go methods have the return type as the last parameter_list or a
        single type_identifier after the parameters.
        """
        # Find the result/return type — it's the node after the parameters
        param_lists = [c for c in func_node.children if c.type == "parameter_list"]

        if len(param_lists) >= 2:
            # Second parameter_list is the return types
            return_list = param_lists[-1]
            # Skip if it's the receiver (method_declaration has receiver first)
            if func_node.type == "method_declaration" and len(param_lists) >= 3:
                return_list = param_lists[-1]

            for child in return_list.children:
                if child.type == "parameter_declaration":
                    self._extract_param_declaration_types(child, result, "return")
                elif child.type in ("type_identifier", "pointer_type", "qualified_type"):
                    type_name = self._extract_type_name_from_node(child)
                    if type_name and type_name not in _BUILTIN_TYPES:
                        result.type_refs.append(
                            TypeRef(
                                name=type_name,
                                kind="return",
                                line=child.start_point[0] + 1,
                            )
                        )

        # Single return type (no parentheses)
        for child in func_node.children:
            if child == func_node.child_by_field_name("name"):
                continue
            if child.type in ("type_identifier", "pointer_type", "qualified_type"):
                # This is a return type if it appears after the parameter lists
                if all(
                    child.start_byte > p.end_byte
                    for p in param_lists[:2] if param_lists
                ):
                    type_name = self._extract_type_name_from_node(child)
                    if type_name and type_name not in _BUILTIN_TYPES:
                        result.type_refs.append(
                            TypeRef(
                                name=type_name,
                                kind="return",
                                line=child.start_point[0] + 1,
                            )
                        )

    def _extract_param_declaration_types(
        self, param_node: Node, result: ParseResult, kind: str
    ) -> None:
        """Extract type references from a parameter_declaration node."""
        param_name = ""
        for child in param_node.children:
            if child.type == "identifier":
                param_name = child.text.decode("utf-8")
                break

        # Find the type node(s) in the parameter declaration
        for child in param_node.children:
            if child.type in (
                "type_identifier",
                "pointer_type",
                "qualified_type",
                "slice_type",
                "map_type",
                "interface_type",
                "struct_type",
                "function_type",
                "channel_type",
            ):
                type_name = self._extract_type_name_from_node(child)
                if type_name and type_name not in _BUILTIN_TYPES:
                    result.type_refs.append(
                        TypeRef(
                            name=type_name,
                            kind=kind,
                            line=child.start_point[0] + 1,
                            param_name=param_name,
                        )
                    )

    def _extract_var_types(self, node: Node, result: ParseResult) -> None:
        """Extract type references from var declarations."""
        for child in node.children:
            if child.type == "var_spec":
                for sub in child.children:
                    if sub.type in ("type_identifier", "pointer_type", "qualified_type"):
                        type_name = self._extract_type_name_from_node(sub)
                        if type_name and type_name not in _BUILTIN_TYPES:
                            result.type_refs.append(
                                TypeRef(
                                    name=type_name,
                                    kind="variable",
                                    line=sub.start_point[0] + 1,
                                )
                            )

    # ------------------------------------------------------------------
    # Signature builders
    # ------------------------------------------------------------------

    def _build_function_signature(self, node: Node, content: str) -> str:
        """Build a human-readable signature for a function."""
        name_node = node.child_by_field_name("name")
        params = node.child_by_field_name("parameters")
        if name_node is None or params is None:
            return ""

        name = name_node.text.decode("utf-8")
        params_text = params.text.decode("utf-8")

        # Find return type
        return_text = self._extract_return_text(node, params)
        sig = f"func {name}{params_text}"
        if return_text:
            sig += f" {return_text}"
        return sig

    def _build_method_signature(
        self, node: Node, content: str, receiver_type: str
    ) -> str:
        """Build a human-readable signature for a method."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return ""

        name = name_node.text.decode("utf-8")

        # Find parameter list (not the receiver)
        param_lists = [c for c in node.children if c.type == "parameter_list"]
        params_text = ""
        if len(param_lists) >= 2:
            params_text = param_lists[1].text.decode("utf-8")

        return_text = self._extract_return_text(node, param_lists[1] if len(param_lists) >= 2 else None)
        sig = f"func ({receiver_type}) {name}{params_text}"
        if return_text:
            sig += f" {return_text}"
        return sig

    @staticmethod
    def _extract_return_text(func_node: Node, after_node: Node | None) -> str:
        """Extract the return type text from a function node."""
        if after_node is None:
            return ""

        # Look for nodes after the parameter list that represent return types
        found_params = False
        parts: list[str] = []
        for child in func_node.children:
            if child == after_node:
                found_params = True
                continue
            if found_params and child.type == "block":
                break
            if found_params and child.is_named:
                parts.append(child.text.decode("utf-8"))

        return " ".join(parts) if parts else ""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_type_name_from_node(node: Node) -> str:
        """Extract the primary type name from a type node.

        Handles: type_identifier, pointer_type (*T), qualified_type (pkg.T),
        slice_type ([]T), map_type (map[K]V), etc.
        Returns the first meaningful type identifier found.
        """
        if node.type == "type_identifier":
            return node.text.decode("utf-8")

        if node.type == "pointer_type":
            # *UserService -> "UserService"
            for child in node.children:
                if child.type == "type_identifier":
                    return child.text.decode("utf-8")
                if child.type == "qualified_type":
                    return GoParser._extract_type_name_from_node(child)

        if node.type == "qualified_type":
            # context.Context -> extract "Context" (the selector)
            # But for local packages, return the qualified name
            parts = node.text.decode("utf-8").split(".")
            if len(parts) == 2:
                pkg, name = parts
                if pkg.lower() in _STDLIB_PACKAGES:
                    return ""  # Skip stdlib types
                return name
            return parts[-1] if parts else ""

        if node.type == "slice_type":
            # []User -> "User"
            for child in node.children:
                result = GoParser._extract_type_name_from_node(child)
                if result:
                    return result

        if node.type == "map_type":
            # map[string]User -> "User" (value type)
            children = [c for c in node.children if c.is_named]
            if len(children) >= 2:
                return GoParser._extract_type_name_from_node(children[-1])

        # Fallback: DFS for first type_identifier
        return GoParser._find_first_type_identifier(node)

    @staticmethod
    def _find_first_type_identifier(node: Node) -> str:
        """DFS for the first type_identifier node."""
        if node.type == "type_identifier":
            text = node.text.decode("utf-8")
            if text not in _BUILTIN_TYPES:
                return text
        for child in node.children:
            found = GoParser._find_first_type_identifier(child)
            if found:
                return found
        return ""

    @staticmethod
    def _root_identifier(node: Node) -> str:
        """Walk down to find the leftmost/root identifier in an expression."""
        current = node
        while current is not None:
            if current.type == "identifier":
                return current.text.decode("utf-8")
            if current.type == "field_identifier":
                return current.text.decode("utf-8")
            if current.children:
                current = current.children[0]
            else:
                break
        return ""
